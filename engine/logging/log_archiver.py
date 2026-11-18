"""
engine/logging/log_archiver.py

Tiered log archival: moves old devlog.execution_log rows from PostGIS (hot)
to S3 compressed JSONL (warm) and writes hourly summaries that remain in
PostGIS indefinitely.

Runs as a nightly Airflow DAG task. Can also be run manually.

Archival pipeline per run
-------------------------
1.  Identify rows older than HOT_RETENTION_DAYS (default 30)
2.  Compute hourly summaries: aggregate stats per (service, component,
    operation, city_id, hour_bucket)
3.  Upsert summaries into devlog.execution_summaries
4.  Detect volume anomalies in each hour bucket (flag runaway loops)
5.  Compress raw rows to JSONL.gz and upload to S3
6.  Verify S3 upload (checksum comparison)
7.  Delete raw rows from PostGIS (ONLY after S3 confirmation)
8.  Record archive job result in devlog.archive_jobs

Cost model
----------
PostGIS (RDS Aurora):  ~$0.10/GB/month   — expensive, keep only 30 days
S3 Standard:           ~$0.023/GB/month  — 4x cheaper, keep up to 1 year
S3 Glacier Instant:    ~$0.004/GB/month  — 25x cheaper, keep > 1 year

A typical MEP deployment generating 100MB/day of logs:
  Without archival: 100MB × 365 × $0.10 = ~$3,650/year on PostGIS
  With archival:    30 days × 100MB × $0.10     = $30/year (PostGIS hot)
                  + 335 days × 100MB × $0.023   = $77/year (S3 warm)
                  Total: ~$107/year — 97% cost reduction

Summary rows (kept in PostGIS forever):
  ~1KB per hour-bucket per component → ~100MB/year at typical scale
  Negligible cost. Provides indefinite trend query capability.

Author: MEP Team
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import boto3
from botocore.exceptions import ClientError
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

HOT_RETENTION_DAYS   = 30    # keep raw logs in PostGIS for this many days
WARM_RETENTION_DAYS  = 365   # keep S3 Standard files for this many days
# After WARM_RETENTION_DAYS, Glacier lifecycle rule moves files automatically

# S3 key structure:
# s3://{bucket}/devlogs/{year}/{month}/{day}/{service}/{component}/{hour}h_archive.jsonl.gz
S3_KEY_TEMPLATE = (
    "devlogs/{year}/{month:02d}/{day:02d}/"
    "{service}/{component}/{hour:02d}h_archive.jsonl.gz"
)

# Volume anomaly: flag hours where log count > N × rolling average
ANOMALY_THRESHOLD_MULTIPLIER = 5.0

# ---------------------------------------------------------------------------
# S3 upload retry policy
# ---------------------------------------------------------------------------

# Maximum number of upload attempts per bucket before escalating to CRITICAL
S3_MAX_ATTEMPTS = 3

# Base delay (seconds) for exponential backoff between retries
# Attempt 1 fails → wait 2s → attempt 2
# Attempt 2 fails → wait 4s → attempt 3
# Attempt 3 fails → escalate, do not delete, write failed_upload record
S3_RETRY_BASE_DELAY_SECONDS = 2

# After this many consecutive nightly failures on the same bucket,
# the archival job writes a CRITICAL alert and skips that bucket
# entirely to avoid blocking other buckets from archiving.
# A developer must manually resolve and re-trigger archival for the blocked bucket.
S3_ESCALATION_THRESHOLD = 3  # days of consecutive failures before CRITICAL


# ---------------------------------------------------------------------------
# Archive job result
# ---------------------------------------------------------------------------

@dataclass
class UploadFailure:
    """
    Records a single S3 upload failure after all retries are exhausted.
    Stored in devlog.failed_uploads so the archival job can retry
    these specific buckets on subsequent runs without reprocessing
    everything from scratch.
    """
    service:      str
    component:    str
    hour_bucket:  datetime
    s3_key:       str
    n_attempts:   int
    last_error:   str
    consecutive_daily_failures: int = 1


@dataclass
class ArchiveJobResult:
    job_id:                 str
    started_at:             datetime
    completed_at:           Optional[datetime]
    status:                 str
    archive_window_start:   datetime
    archive_window_end:     datetime
    n_rows_summarised:      int
    n_summaries_written:    int
    n_rows_archived_to_s3:  int
    n_rows_deleted_from_pg: int
    total_compressed_mb:    float
    n_volume_anomalies:     int
    # Rows whose S3 upload failed after all retries.
    # These rows remain in PostGIS and are NOT deleted.
    # They will be retried on the next archival run.
    n_rows_upload_failed:   int = 0
    upload_failures:        list[UploadFailure] = field(default_factory=list)
    error_message:          Optional[str] = None


# ---------------------------------------------------------------------------
# Log archiver
# ---------------------------------------------------------------------------

class LogArchiver:
    """
    Moves aged devlog.execution_log rows from PostGIS to S3.
    Writes hourly summaries before deleting raw rows.
    """

    def __init__(
        self,
        db_url:          str,
        s3_bucket:       str,
        s3_region:       str = "us-east-1",
        hot_retention_days: int = HOT_RETENTION_DAYS,
        dry_run:         bool = False,
    ) -> None:
        self.db_url            = db_url
        self.s3_bucket         = s3_bucket
        self.s3_region         = s3_region
        self.hot_retention_days = hot_retention_days
        self.dry_run           = dry_run  # if True: compute and log but don't delete

        self._engine: Engine = create_engine(db_url)
        self._s3 = boto3.client("s3", region_name=s3_region)

    def run(self) -> ArchiveJobResult:
        """
        Execute the full archival pipeline.
        Called by Airflow nightly or triggered manually.
        """
        job_id     = str(uuid.uuid4())
        started_at = datetime.now(timezone.utc)
        cutoff     = started_at - timedelta(days=self.hot_retention_days)

        logger.info(
            "Log archival starting | job_id=%s | cutoff=%s | dry_run=%s",
            job_id, cutoff.isoformat(), self.dry_run
        )

        result = ArchiveJobResult(
            job_id=job_id,
            started_at=started_at,
            completed_at=None,
            status="running",
            archive_window_start=cutoff - timedelta(days=1),
            archive_window_end=cutoff,
            n_rows_summarised=0,
            n_summaries_written=0,
            n_rows_archived_to_s3=0,
            n_rows_deleted_from_pg=0,
            total_compressed_mb=0.0,
            n_volume_anomalies=0,
        )

        self._write_archive_job_start(result)

        try:
            # Step 1: compute hourly summaries
            summaries = self._compute_hourly_summaries(cutoff)
            result.n_rows_summarised = sum(s["n_executions"] for s in summaries)
            logger.info("Computed %d summary rows from %d log records",
                        len(summaries), result.n_rows_summarised)

            # Step 2: detect volume anomalies in summaries
            anomalies = self._flag_volume_anomalies(summaries)
            result.n_volume_anomalies = len(anomalies)
            if anomalies:
                logger.warning(
                    "%d hour-buckets have volume anomalies. "
                    "Review devlog.execution_summaries WHERE volume_anomaly=TRUE.",
                    len(anomalies)
                )

            # Step 3: upsert summaries to PostGIS
            if not self.dry_run:
                self._upsert_summaries(summaries)
                result.n_summaries_written = len(summaries)

            # Step 4: archive raw rows to S3 by hour bucket
            compressed_mb, n_archived, upload_failures = self._archive_to_s3(
                cutoff, summaries
            )
            result.n_rows_archived_to_s3 = n_archived
            result.total_compressed_mb   = compressed_mb
            result.upload_failures       = upload_failures
            result.n_rows_upload_failed  = sum(
                1 for _ in upload_failures   # one failure entry per bucket
            )

            if upload_failures:
                logger.warning(
                    "%d hour-buckets failed S3 upload after %d attempts each. "
                    "Affected rows remain in PostGIS and will be retried next run. "
                    "See devlog.failed_uploads for details.",
                    len(upload_failures), S3_MAX_ATTEMPTS
                )

            # Step 5: delete raw rows from PostGIS (only after S3 confirmed)
            if not self.dry_run:
                n_deleted, n_retained = self._delete_archived_rows(cutoff)
                result.n_rows_deleted_from_pg = n_deleted
                logger.info(
                    "Deleted %d archived rows from PostGIS | "
                    "%d rows retained pending upload confirmation.",
                    n_deleted, n_retained
                )

            result.status        = "completed"
            result.completed_at  = datetime.now(timezone.utc)

        except Exception as e:
            result.status        = "failed"
            result.error_message = str(e)
            result.completed_at  = datetime.now(timezone.utc)
            logger.error("Log archival failed: %s", e, exc_info=True)

        finally:
            self._write_archive_job_complete(result)

        logger.info(
            "Log archival complete | status=%s | "
            "summarised=%d | archived=%d | deleted=%d | size=%.1fMB",
            result.status, result.n_rows_summarised,
            result.n_rows_archived_to_s3, result.n_rows_deleted_from_pg,
            result.total_compressed_mb
        )

        return result

    # ==========================================================================
    # Step 1: Compute hourly summaries
    # ==========================================================================

    def _compute_hourly_summaries(self, cutoff: datetime) -> list[dict]:
        """
        Aggregate execution_log rows older than cutoff into hourly buckets.
        Uses PostgreSQL PERCENTILE_CONT for accurate p50/p95/p99 computation
        — more accurate than Python-side approximation on large datasets.
        """
        with self._engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT
                    DATE_TRUNC('hour', logged_at)       AS hour_bucket,
                    service,
                    component,
                    operation,
                    city_id::TEXT,

                    COUNT(*)                                     AS n_executions,
                    SUM(CASE WHEN level = 'ERROR'    THEN 1 ELSE 0 END) AS n_errors,
                    SUM(CASE WHEN level = 'WARNING'  THEN 1 ELSE 0 END) AS n_warnings,

                    MIN(duration_ms)                             AS duration_min_ms,
                    MAX(duration_ms)                             AS duration_max_ms,
                    AVG(duration_ms)                             AS duration_avg_ms,
                    PERCENTILE_CONT(0.50) WITHIN GROUP
                        (ORDER BY duration_ms)                   AS duration_p50_ms,
                    PERCENTILE_CONT(0.95) WITHIN GROUP
                        (ORDER BY duration_ms)                   AS duration_p95_ms,
                    PERCENTILE_CONT(0.99) WITHIN GROUP
                        (ORDER BY duration_ms)                   AS duration_p99_ms,

                    SUM(COALESCE(rows_processed, 0))             AS total_rows_processed,
                    AVG(rows_per_second)                         AS avg_rows_per_second,

                    AVG(memory_mb)                               AS memory_avg_mb,
                    MAX(memory_mb)                               AS memory_max_mb

                FROM devlog.execution_log
                WHERE logged_at < :cutoff
                GROUP BY
                    DATE_TRUNC('hour', logged_at),
                    service, component, operation, city_id
                ORDER BY hour_bucket, service, component
            """), {"cutoff": cutoff})

            return [dict(r._mapping) for r in rows]

    # ==========================================================================
    # Step 2: Detect volume anomalies
    # ==========================================================================

    def _flag_volume_anomalies(self, summaries: list[dict]) -> list[dict]:
        """
        Compare each hour-bucket's execution count to the rolling 30-day average
        for the same (service, component, operation). Flag anomalous hours.

        A rolling average is computed from existing execution_summaries rows
        (already summarised periods). This means the check is fast — it queries
        the compact summaries table, not the raw log.
        """
        anomalies = []

        with self._engine.connect() as conn:
            # Get rolling baselines for all (service, component, operation) tuples
            baselines = conn.execute(text("""
                SELECT
                    service, component, operation,
                    AVG(n_executions) AS avg_per_hour,
                    STDDEV(n_executions) AS stddev_per_hour
                FROM devlog.execution_summaries
                WHERE hour_bucket >= NOW() - INTERVAL '30 days'
                GROUP BY service, component, operation
            """))
            baseline_map = {
                (r.service, r.component, r.operation): {
                    "avg": float(r.avg_per_hour or 0),
                    "std": float(r.stddev_per_hour or 0),
                }
                for r in baselines
            }

        for summary in summaries:
            key = (summary["service"], summary["component"], summary["operation"])
            baseline = baseline_map.get(key)

            if baseline and baseline["avg"] > 0:
                ratio = summary["n_executions"] / baseline["avg"]
                if ratio > ANOMALY_THRESHOLD_MULTIPLIER:
                    summary["volume_anomaly"] = True
                    summary["volume_anomaly_note"] = (
                        f"n_executions={summary['n_executions']} is {ratio:.1f}x "
                        f"the 30-day average of {baseline['avg']:.0f}/hour. "
                        f"Possible runaway loop in {summary['component']}.{summary['operation']}."
                    )
                    anomalies.append(summary)
                else:
                    summary["volume_anomaly"] = False
                    summary["volume_anomaly_note"] = None
            else:
                summary["volume_anomaly"] = False
                summary["volume_anomaly_note"] = None

        return anomalies

    # ==========================================================================
    # Step 3: Upsert summaries
    # ==========================================================================

    def _upsert_summaries(self, summaries: list[dict]) -> None:
        """Write computed summaries to devlog.execution_summaries."""
        if not summaries:
            return

        with self._engine.begin() as conn:
            for s in summaries:
                conn.execute(text("""
                    INSERT INTO devlog.execution_summaries (
                        hour_bucket, service, component, operation, city_id,
                        n_executions, n_errors, n_warnings,
                        duration_min_ms, duration_max_ms, duration_avg_ms,
                        duration_p50_ms, duration_p95_ms, duration_p99_ms,
                        total_rows_processed, avg_rows_per_second,
                        memory_avg_mb, memory_max_mb,
                        volume_anomaly, volume_anomaly_note
                    ) VALUES (
                        :hour_bucket, :service, :component, :operation,
                        :city_id::UUID,
                        :n_executions, :n_errors, :n_warnings,
                        :duration_min_ms, :duration_max_ms, :duration_avg_ms,
                        :duration_p50_ms, :duration_p95_ms, :duration_p99_ms,
                        :total_rows_processed, :avg_rows_per_second,
                        :memory_avg_mb, :memory_max_mb,
                        :volume_anomaly, :volume_anomaly_note
                    )
                    ON CONFLICT (hour_bucket, service, component, operation, city_id)
                    DO UPDATE SET
                        n_executions         = EXCLUDED.n_executions,
                        n_errors             = EXCLUDED.n_errors,
                        volume_anomaly       = EXCLUDED.volume_anomaly,
                        volume_anomaly_note  = EXCLUDED.volume_anomaly_note
                """), {
                    **s,
                    "city_id": s.get("city_id"),
                })

    # ==========================================================================
    # Step 4: Archive raw rows to S3 with retry logic
    # ==========================================================================

    def _archive_to_s3(
        self,
        cutoff: datetime,
        summaries: list[dict]
    ) -> tuple[float, int, list[UploadFailure]]:
        """
        Upload log rows grouped by (service, component, hour_bucket) to S3
        as compressed JSONL files.

        Returns (total_compressed_mb, total_rows_uploaded, upload_failures).

        Retry behaviour
        ---------------
        Each bucket is attempted up to S3_MAX_ATTEMPTS times with exponential
        backoff. If all attempts fail:
          - The failure is recorded in devlog.failed_uploads
          - The bucket's rows are NOT deleted from PostGIS
          - The job continues with the next bucket (one failure does not
            block all other buckets from archiving)
          - If the same bucket has failed on S3_ESCALATION_THRESHOLD
            consecutive days, a CRITICAL alert is written

        Upload verification
        -------------------
        After each successful PUT, we verify the uploaded object's ETag
        against the MD5 of the compressed data. S3 returns the MD5 as the
        ETag for single-part uploads. If they disagree, we treat the upload
        as failed and retry.
        """
        total_compressed_bytes = 0
        total_rows             = 0
        upload_failures        = []

        with self._engine.connect() as conn:
            buckets = conn.execute(text("""
                SELECT DISTINCT
                    DATE_TRUNC('hour', logged_at) AS hour_bucket,
                    service,
                    component
                FROM devlog.execution_log
                WHERE logged_at < :cutoff
                ORDER BY hour_bucket, service, component
            """), {"cutoff": cutoff})
            bucket_list = [dict(r._mapping) for r in buckets]

        # Also pick up any previously failed buckets from prior runs
        # so they get retried even if they fall outside the current cutoff window
        prior_failures = self._load_pending_failed_uploads()
        seen_buckets   = {
            (b["service"], b["component"], b["hour_bucket"])
            for b in bucket_list
        }
        for pf in prior_failures:
            key = (pf["service"], pf["component"], pf["hour_bucket"])
            if key not in seen_buckets:
                bucket_list.append(pf)
                seen_buckets.add(key)

        for bucket in bucket_list:
            hour    = bucket["hour_bucket"]
            service = bucket["service"]
            comp    = bucket["component"]

            with self._engine.connect() as conn:
                rows = conn.execute(text("""
                    SELECT
                        log_id::TEXT, logged_at, level, service, component,
                        operation, run_id::TEXT, city_id::TEXT,
                        duration_ms, memory_mb, rows_processed,
                        error_type, error_message, stack_trace,
                        error_code, context, host_name, pod_name,
                        git_commit, app_version
                    FROM devlog.execution_log
                    WHERE logged_at >= :start
                      AND logged_at <  :end
                      AND service   =  :service
                      AND component =  :component
                    ORDER BY logged_at
                """), {
                    "start":     hour,
                    "end":       hour + timedelta(hours=1),
                    "service":   service,
                    "component": comp,
                })
                raw_rows = [dict(r._mapping) for r in rows]

            if not raw_rows:
                continue

            jsonl_bytes = "\n".join(
                json.dumps(row, default=str) for row in raw_rows
            ).encode("utf-8")

            compressed_buf = io.BytesIO()
            with gzip.GzipFile(fileobj=compressed_buf, mode="wb") as gz:
                gz.write(jsonl_bytes)
            compressed_data = compressed_buf.getvalue()

            # Compute MD5 for upload verification
            compressed_md5 = hashlib.md5(compressed_data).hexdigest()

            s3_key = S3_KEY_TEMPLATE.format(
                year=hour.year, month=hour.month, day=hour.day,
                service=service, component=comp, hour=hour.hour
            )

            if self.dry_run:
                total_compressed_bytes += len(compressed_data)
                total_rows += len(raw_rows)
                continue

            # -----------------------------------------------------------------
            # Retry loop
            # -----------------------------------------------------------------
            uploaded   = False
            last_error = ""

            for attempt in range(1, S3_MAX_ATTEMPTS + 1):
                try:
                    response = self._s3.put_object(
                        Bucket=self.s3_bucket,
                        Key=s3_key,
                        Body=compressed_data,
                        ContentType="application/gzip",
                        ContentEncoding="gzip",
                        Metadata={
                            "service":     service,
                            "component":   comp,
                            "hour_bucket": hour.isoformat(),
                            "row_count":   str(len(raw_rows)),
                            "md5":         compressed_md5,
                        },
                    )

                    # Verify: S3 ETag for single-part upload == MD5 of the body
                    # S3 wraps the hex MD5 in quotes: '"abc123..."'
                    etag = response.get("ETag", "").strip('"')
                    if etag and etag != compressed_md5:
                        raise ValueError(
                            f"Upload verification failed: "
                            f"ETag={etag} does not match local MD5={compressed_md5}. "
                            f"Data may be corrupted in transit."
                        )

                    # S3 upload confirmed — update the summary with the archive key
                    with self._engine.begin() as conn:
                        conn.execute(text("""
                            UPDATE devlog.execution_summaries
                            SET s3_archive_key = :s3_key,
                                archived_at    = NOW()
                            WHERE hour_bucket = :hour
                              AND service     = :service
                              AND component   = :component
                        """), {
                            "s3_key":    f"s3://{self.s3_bucket}/{s3_key}",
                            "hour":      hour,
                            "service":   service,
                            "component": comp,
                        })

                    # Clear any prior failure record for this bucket
                    self._clear_failed_upload(service, comp, hour)

                    total_compressed_bytes += len(compressed_data)
                    total_rows += len(raw_rows)
                    uploaded = True

                    logger.debug(
                        "S3 upload confirmed | attempt=%d/%d | %s/%s/%s → %s (%.1f KB)",
                        attempt, S3_MAX_ATTEMPTS,
                        service, comp, hour.strftime("%Y-%m-%d %H"),
                        s3_key, len(compressed_data) / 1024
                    )
                    break  # success — exit retry loop

                except (ClientError, ValueError, OSError) as exc:
                    last_error = str(exc)
                    logger.warning(
                        "S3 upload attempt %d/%d failed | %s/%s/%s | error: %s",
                        attempt, S3_MAX_ATTEMPTS,
                        service, comp, hour.strftime("%Y-%m-%d %H"),
                        last_error
                    )

                    if attempt < S3_MAX_ATTEMPTS:
                        delay = S3_RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1))
                        logger.info(
                            "Retrying S3 upload in %.0fs (exponential backoff)...",
                            delay
                        )
                        time.sleep(delay)
                    # else: fall through to failure handling below

            # -----------------------------------------------------------------
            # All attempts exhausted — record failure, do NOT delete rows
            # -----------------------------------------------------------------
            if not uploaded:
                consecutive = self._record_failed_upload(
                    service=service,
                    component=comp,
                    hour_bucket=hour,
                    s3_key=s3_key,
                    n_attempts=S3_MAX_ATTEMPTS,
                    last_error=last_error,
                )

                failure = UploadFailure(
                    service=service,
                    component=comp,
                    hour_bucket=hour,
                    s3_key=s3_key,
                    n_attempts=S3_MAX_ATTEMPTS,
                    last_error=last_error,
                    consecutive_daily_failures=consecutive,
                )
                upload_failures.append(failure)

                # Rows for this bucket stay in PostGIS.
                # They will be retried next run when _load_pending_failed_uploads
                # picks them up. This is NOT a skip — it is a tracked deferral.

                logger.error(
                    "S3 upload FAILED after %d attempts | %s/%s/%s | "
                    "Rows will remain in PostGIS and be retried next run. "
                    "Consecutive daily failures: %d. "
                    "Error: %s",
                    S3_MAX_ATTEMPTS, service, comp,
                    hour.strftime("%Y-%m-%d %H"),
                    consecutive, last_error
                )

                if consecutive >= S3_ESCALATION_THRESHOLD:
                    logger.critical(
                        "ESCALATION: S3 upload for %s/%s/%s has failed on %d "
                        "consecutive days. Hot storage is accumulating. "
                        "Manual intervention required. "
                        "Check S3 bucket permissions, network connectivity, "
                        "and bucket lifecycle policies.",
                        service, comp, hour.strftime("%Y-%m-%d %H"), consecutive
                    )
                    self._write_escalation_alert(failure)

        return (
            total_compressed_bytes / (1024 * 1024),
            total_rows,
            upload_failures
        )

    # ==========================================================================
    # Step 5: Delete archived rows — only rows with confirmed S3 upload
    # ==========================================================================

    def _delete_archived_rows(self, cutoff: datetime) -> tuple[int, int]:
        """
        Delete raw log rows that have confirmed S3 uploads.

        Returns (n_deleted, n_retained_pending_upload).

        Only deletes rows where:
          1. The corresponding execution_summaries row has s3_archive_key IS NOT NULL
          2. AND archived_at IS NOT NULL
          3. AND the row is not in devlog.failed_uploads

        Rows without confirmed S3 uploads are NOT deleted — they remain in
        PostGIS until the upload succeeds on a future run. This is a tracked
        deferral, not a silent skip. The caller receives n_retained_pending_upload
        so it can be reported in the job result and monitored.
        """
        with self._engine.begin() as conn:
            # Delete rows with confirmed S3 upload
            delete_result = conn.execute(text("""
                DELETE FROM devlog.execution_log
                WHERE logged_at < :cutoff
                  AND EXISTS (
                      SELECT 1
                      FROM devlog.execution_summaries es
                      WHERE es.service    = execution_log.service
                        AND es.component  = execution_log.component
                        AND es.hour_bucket = DATE_TRUNC('hour', execution_log.logged_at)
                        AND es.s3_archive_key IS NOT NULL
                        AND es.archived_at   IS NOT NULL
                  )
                  -- Explicitly exclude rows in pending failed uploads
                  -- even if a stale summary somehow has an archive key
                  AND NOT EXISTS (
                      SELECT 1
                      FROM devlog.failed_uploads fu
                      WHERE fu.service    = execution_log.service
                        AND fu.component  = execution_log.component
                        AND fu.hour_bucket = DATE_TRUNC('hour', execution_log.logged_at)
                        AND fu.resolved_at IS NULL
                  )
            """), {"cutoff": cutoff})
            n_deleted = delete_result.rowcount

        # Count rows still in PostGIS past cutoff (pending upload confirmation)
        with self._engine.connect() as conn:
            retained = conn.execute(text("""
                SELECT COUNT(*) AS n
                FROM devlog.execution_log
                WHERE logged_at < :cutoff
            """), {"cutoff": cutoff})
            n_retained = retained.scalar() or 0

        if n_retained > 0:
            logger.warning(
                "%d log rows remain in PostGIS past the retention cutoff "
                "(%s) pending S3 upload confirmation. "
                "These will be retried on the next archival run. "
                "Check devlog.failed_uploads for details.",
                n_retained, cutoff.isoformat()
            )

        return n_deleted, n_retained

    # ==========================================================================
    # Failed upload tracking — persistence layer
    # ==========================================================================

    def _record_failed_upload(
        self,
        service:     str,
        component:   str,
        hour_bucket: datetime,
        s3_key:      str,
        n_attempts:  int,
        last_error:  str,
    ) -> int:
        """
        Upsert a failed upload record. Returns the updated consecutive
        daily failure count for this bucket.
        """
        with self._engine.begin() as conn:
            result = conn.execute(text("""
                INSERT INTO devlog.failed_uploads (
                    service, component, hour_bucket, s3_key,
                    n_attempts, last_error,
                    first_failed_at, last_failed_at,
                    consecutive_daily_failures
                ) VALUES (
                    :service, :component, :hour_bucket, :s3_key,
                    :n_attempts, :last_error,
                    NOW(), NOW(), 1
                )
                ON CONFLICT (service, component, hour_bucket)
                DO UPDATE SET
                    n_attempts                 = failed_uploads.n_attempts
                                                 + EXCLUDED.n_attempts,
                    last_error                 = EXCLUDED.last_error,
                    last_failed_at             = NOW(),
                    consecutive_daily_failures = failed_uploads.consecutive_daily_failures + 1
                RETURNING consecutive_daily_failures
            """), {
                "service":     service,
                "component":   component,
                "hour_bucket": hour_bucket,
                "s3_key":      s3_key,
                "n_attempts":  n_attempts,
                "last_error":  last_error,
            })
            return result.scalar() or 1

    def _clear_failed_upload(
        self,
        service:     str,
        component:   str,
        hour_bucket: datetime,
    ) -> None:
        """Mark a previously failed upload as resolved after successful retry."""
        with self._engine.begin() as conn:
            conn.execute(text("""
                UPDATE devlog.failed_uploads
                SET resolved_at = NOW()
                WHERE service     = :service
                  AND component   = :component
                  AND hour_bucket = :hour_bucket
                  AND resolved_at IS NULL
            """), {
                "service":     service,
                "component":   component,
                "hour_bucket": hour_bucket,
            })

    def _load_pending_failed_uploads(self) -> list[dict]:
        """
        Load all unresolved failed upload records so they can be retried
        in the current archival run regardless of whether they fall within
        the current cutoff window.
        """
        with self._engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT service, component, hour_bucket, s3_key,
                       consecutive_daily_failures
                FROM devlog.failed_uploads
                WHERE resolved_at IS NULL
                ORDER BY hour_bucket
            """))
            return [dict(r._mapping) for r in rows]

    def _write_escalation_alert(self, failure: UploadFailure) -> None:
        """
        Write a CRITICAL escalation alert to devlog.log_volume_alerts
        (reused as the general operations alert channel).
        """
        try:
            with self._engine.begin() as conn:
                conn.execute(text("""
                    INSERT INTO devlog.log_volume_alerts (
                        service, component, operation,
                        window_start, window_end, window_minutes,
                        n_logs_in_window, severity,
                        throttle_applied, throttle_action
                    ) VALUES (
                        :service, :component, 'S3_UPLOAD',
                        :hour_bucket, :hour_bucket + INTERVAL '1 hour', 60,
                        0, 'CRITICAL',
                        FALSE,
                        'S3_UPLOAD_ESCALATION: ' || :consecutive ||
                        ' consecutive daily failures. Manual intervention required.'
                    )
                """), {
                    "service":     failure.service,
                    "component":   failure.component,
                    "hour_bucket": failure.hour_bucket,
                    "consecutive": failure.consecutive_daily_failures,
                })
        except Exception as exc:
            logger.error("Failed to write escalation alert: %s", exc)

    # ==========================================================================
    # Archive job tracking
    # ==========================================================================

    def _write_archive_job_start(self, result: ArchiveJobResult) -> None:
        with self._engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO devlog.archive_jobs (
                    job_id, started_at, status,
                    archive_window_start, archive_window_end,
                    s3_bucket
                ) VALUES (
                    :job_id::UUID, :started_at, 'running',
                    :window_start, :window_end, :s3_bucket
                )
            """), {
                "job_id":       result.job_id,
                "started_at":   result.started_at,
                "window_start": result.archive_window_start,
                "window_end":   result.archive_window_end,
                "s3_bucket":    self.s3_bucket,
            })

    def _write_archive_job_complete(self, result: ArchiveJobResult) -> None:
        with self._engine.begin() as conn:
            conn.execute(text("""
                UPDATE devlog.archive_jobs
                SET status                   = :status,
                    completed_at             = :completed_at,
                    n_rows_summarised        = :n_rows_summarised,
                    n_summaries_written      = :n_summaries_written,
                    n_rows_archived_to_s3    = :n_rows_archived_to_s3,
                    n_rows_deleted_from_pg   = :n_rows_deleted_from_pg,
                    total_compressed_mb      = :total_compressed_mb,
                    n_volume_anomalies       = :n_volume_anomalies,
                    error_message            = :error_message
                WHERE job_id = :job_id::UUID
            """), {
                "job_id":               result.job_id,
                "status":               result.status,
                "completed_at":         result.completed_at,
                "n_rows_summarised":    result.n_rows_summarised,
                "n_summaries_written":  result.n_summaries_written,
                "n_rows_archived_to_s3":  result.n_rows_archived_to_s3,
                "n_rows_deleted_from_pg": result.n_rows_deleted_from_pg,
                "total_compressed_mb":  result.total_compressed_mb,
                "n_volume_anomalies":   result.n_volume_anomalies,
                "error_message":        result.error_message,
            })

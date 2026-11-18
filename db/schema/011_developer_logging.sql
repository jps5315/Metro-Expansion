-- =============================================================================
-- 011_developer_logging.sql
-- Developer-facing structured technical log schema
--
-- Completely separate from audit.event_log and audit.anomaly_log.
-- Those tables are business records for planners and legal compliance.
-- This schema is an operational record for developers and DevOps.
--
-- Separation rationale
-- --------------------
-- Planner logs answer: "what did a planner decide and when?"
-- Developer logs answer: "what did the code execute, how long did it take,
-- and what failed?"
--
-- Planners MUST NOT see developer logs because:
--   - Stack traces and SQL queries reveal implementation details
--   - Performance metrics are meaningless to non-technical users
--   - Log volume from a buggy loop would bury business events
--
-- Developers MUST NOT rely on planner audit logs because:
--   - Audit logs are immutable and cannot be pruned or summarised
--   - They contain no timing, memory, or execution detail
--   - Querying them for debugging requires joining through business entities
--
-- Tiered storage strategy
-- -----------------------
-- Hot  (devlog schema, PostGIS)   last 30 days   fully queryable
-- Warm (S3, compressed JSONL)     30d – 1 year   queryable via Athena
-- Cold (S3 Glacier)               > 1 year       archival only
--
-- When logs age from hot to warm:
--   - Raw log rows are compressed and uploaded to S3
--   - Hourly summary rows are written to devlog.execution_summaries
--   - Raw rows are deleted from PostGIS (freeing hot storage)
--   - The summary remains queryable in PostGIS indefinitely
--
-- This means a developer can always answer "how many gravity model runs
-- occurred in March 2025 and what was the average duration?" without
-- paying for a full PostGIS scan of millions of raw rows.
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS devlog;

-- =============================================================================
-- execution_log
-- One row per significant code execution event.
-- Written by the Python structured logger in engine/logging/structured_logger.py
-- Covers: analysis pipeline steps, database queries, spatial operations,
--         API requests, background jobs, data ingestion steps.
-- =============================================================================
CREATE TABLE devlog.execution_log (
    log_id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    logged_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Source identification
    service         TEXT NOT NULL,
    -- 'suitability_engine' | 'data_ingestion' | 'api_server' |
    -- 'validation' | 'scheduler' | 'projection_engine'

    component       TEXT NOT NULL,
    -- e.g. 'HospitalSuitabilityAnalyzer' | 'DataQualityValidator' |
    --       'ProjectionStrategy' | 'GravityCatchmentScorer'

    operation       TEXT NOT NULL,
    -- e.g. 'load_layers' | 'apply_hard_constraints' | '_score_gravity_catchment'
    --       'run_index_reference_check' | 'validate_land_parcels'

    -- Log level
    level           TEXT NOT NULL DEFAULT 'INFO',
    -- 'DEBUG' | 'INFO' | 'WARNING' | 'ERROR' | 'CRITICAL'

    -- Correlation identifiers — link log rows to business entities
    -- All nullable: not every log event is tied to a specific run
    run_id          UUID,       -- analysis_runs.run_id
    city_id         UUID,       -- spatial.cities.city_id
    scenario_id     UUID,       -- analysis.expansion_scenarios.scenario_id
    request_id      TEXT,       -- API request UUID (for HTTP request tracing)
    worker_id       TEXT,       -- Celery/Spark worker identifier
    trace_id        TEXT,       -- distributed trace ID (OpenTelemetry)
    span_id         TEXT,       -- span within the trace

    -- Execution metrics
    duration_ms     NUMERIC(12, 3),   -- wall-clock execution time
    cpu_ms          NUMERIC(12, 3),   -- CPU time (from resource.getrusage)
    memory_mb       NUMERIC(10, 3),   -- RSS memory at log time
    rows_processed  INTEGER,          -- rows/features processed in this step
    rows_per_second NUMERIC(14, 3),   -- throughput

    -- Error detail (NULL when level != ERROR/CRITICAL)
    error_type      TEXT,             -- exception class name
    error_message   TEXT,             -- exception message (no stack trace here)
    stack_trace     TEXT,             -- full stack trace (developer eyes only)
    error_code      TEXT,             -- application error code e.g. 'GEOM_INVALID'

    -- Structured context — arbitrary key-value pairs specific to the operation
    -- Examples:
    --   load_layers:              {"n_parcels": 847231, "crs": "UTM18N"}
    --   _score_gravity_catchment: {"beta": 1.8, "max_radius_m": 15000, "n_hospitals": 42}
    --   validate_land_parcels:    {"critical_count": 2, "warning_count": 7}
    --   index_reference_check:    {"passed": true, "speedup_factor": 47.3}
    context         JSONB NOT NULL DEFAULT '{}',

    -- Host / environment
    host_name       TEXT,
    pod_name        TEXT,       -- Kubernetes pod name
    git_commit      TEXT,       -- git SHA of running code (populated at deploy)
    app_version     TEXT        -- semantic version of the MEP application
);

-- Primary query patterns:
-- 1. "Show me all errors in the last 24 hours"
CREATE INDEX idx_execlog_recent_errors
    ON devlog.execution_log(logged_at DESC, level)
    WHERE level IN ('ERROR', 'CRITICAL');

-- 2. "Show me all steps for run_id X" (debugging a specific analysis run)
CREATE INDEX idx_execlog_run
    ON devlog.execution_log(run_id, logged_at)
    WHERE run_id IS NOT NULL;

-- 3. "How is _score_gravity_catchment performing?" (performance profiling)
CREATE INDEX idx_execlog_operation
    ON devlog.execution_log(component, operation, logged_at DESC);

-- 4. "What happened on city X yesterday?" (city-scoped debugging)
CREATE INDEX idx_execlog_city_time
    ON devlog.execution_log(city_id, logged_at DESC)
    WHERE city_id IS NOT NULL;

-- 5. Volume anomaly detection (runaway loop detection)
CREATE INDEX idx_execlog_volume_check
    ON devlog.execution_log(service, component, logged_at)
    INCLUDE (log_id);

COMMENT ON TABLE devlog.execution_log IS
    'Structured technical execution log for developers and DevOps. '
    'NOT visible to planners. '
    'Hot tier: last 30 days in PostGIS. '
    'Older rows are summarised to execution_summaries and archived to S3. '
    'stack_trace column is developer-only — never exposed via API.';

-- =============================================================================
-- execution_summaries
-- Hourly aggregate summaries generated when raw logs age out of hot tier.
-- Provides long-term trend data without keeping millions of raw rows.
-- =============================================================================
CREATE TABLE devlog.execution_summaries (
    summary_id      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    hour_bucket     TIMESTAMPTZ NOT NULL,
    -- truncated to the hour: DATE_TRUNC('hour', logged_at)

    service         TEXT NOT NULL,
    component       TEXT NOT NULL,
    operation       TEXT NOT NULL,
    city_id         UUID,

    -- Aggregated metrics for this service+component+operation+hour
    n_executions    INTEGER NOT NULL DEFAULT 0,
    n_errors        INTEGER NOT NULL DEFAULT 0,
    n_warnings      INTEGER NOT NULL DEFAULT 0,

    -- Duration statistics (ms)
    duration_min_ms     NUMERIC(12,3),
    duration_max_ms     NUMERIC(12,3),
    duration_avg_ms     NUMERIC(12,3),
    duration_p50_ms     NUMERIC(12,3),
    duration_p95_ms     NUMERIC(12,3),
    duration_p99_ms     NUMERIC(12,3),

    -- Throughput
    total_rows_processed    BIGINT,
    avg_rows_per_second     NUMERIC(14,3),

    -- Memory (MB)
    memory_avg_mb       NUMERIC(10,3),
    memory_max_mb       NUMERIC(10,3),

    -- Volume anomaly flag
    -- Set by archive job when this hour had anomalous log volume
    -- (> 3 std deviations above rolling 30-day average for this component)
    volume_anomaly      BOOLEAN DEFAULT FALSE,
    volume_anomaly_note TEXT,

    -- S3 archive pointer
    -- When raw logs for this hour are moved to S3, this column is populated
    -- Raw rows can then be safely deleted from PostGIS
    s3_archive_key      TEXT,
    -- e.g. 's3://mep-logs/2026/03/15/suitability_engine/14h_archive.jsonl.gz'
    archived_at         TIMESTAMPTZ,

    CONSTRAINT execution_summaries_unique
        UNIQUE (hour_bucket, service, component, operation, city_id)
);

CREATE INDEX idx_summaries_hour
    ON devlog.execution_summaries(hour_bucket DESC);
CREATE INDEX idx_summaries_component
    ON devlog.execution_summaries(component, operation, hour_bucket DESC);
CREATE INDEX idx_summaries_errors
    ON devlog.execution_summaries(hour_bucket DESC)
    WHERE n_errors > 0;
CREATE INDEX idx_summaries_anomaly
    ON devlog.execution_summaries(volume_anomaly)
    WHERE volume_anomaly = TRUE;

COMMENT ON TABLE devlog.execution_summaries IS
    'Hourly aggregate summaries of execution_log. '
    'Populated by the log archival job when raw logs age out of hot storage. '
    'Retained indefinitely in PostGIS — provides long-term trend data at low cost. '
    's3_archive_key points to the compressed raw log file for that hour in S3.';

-- =============================================================================
-- log_volume_alerts
-- Records when a single source writes anomalously high log volume.
-- Prevents runaway loops from silently consuming cloud storage budget.
-- =============================================================================
CREATE TABLE devlog.log_volume_alerts (
    alert_id        UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    detected_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    service         TEXT NOT NULL,
    component       TEXT NOT NULL,
    operation       TEXT,

    -- Window that triggered the alert
    window_start    TIMESTAMPTZ NOT NULL,
    window_end      TIMESTAMPTZ NOT NULL,
    window_minutes  INTEGER NOT NULL,

    -- Volume metrics
    n_logs_in_window    INTEGER NOT NULL,
    n_logs_expected     INTEGER,         -- based on 30-day rolling baseline
    volume_ratio        NUMERIC(8,2),    -- actual / expected
    estimated_size_mb   NUMERIC(10,3),

    -- Alert severity
    severity            TEXT NOT NULL,
    -- 'NOTICE'   2-5x expected volume — monitor
    -- 'WARNING'  5-20x expected volume — investigate soon
    -- 'CRITICAL' 20x+ expected volume — likely runaway loop, auto-throttle

    -- Auto-throttle action taken
    throttle_applied    BOOLEAN DEFAULT FALSE,
    throttle_action     TEXT,
    -- 'SAMPLE_1_IN_100' | 'SAMPLE_1_IN_1000' | 'DROP_DEBUG' | 'DROP_INFO'
    -- Sampling drops log rows to protect storage while preserving ERROR/CRITICAL

    -- Resolution
    resolved_at         TIMESTAMPTZ,
    resolved_by         TEXT,       -- developer who resolved
    resolution_note     TEXT
);

CREATE INDEX idx_volume_alerts_recent
    ON devlog.log_volume_alerts(detected_at DESC);
CREATE INDEX idx_volume_alerts_unresolved
    ON devlog.log_volume_alerts(detected_at DESC)
    WHERE resolved_at IS NULL;

-- =============================================================================
-- failed_uploads
-- Tracks S3 upload failures so they can be retried on subsequent runs.
-- A row here means: "this bucket's logs are still in PostGIS because
-- we could not confirm they made it to S3."
-- =============================================================================
CREATE TABLE IF NOT EXISTS devlog.failed_uploads (
    failure_id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    service                     TEXT NOT NULL,
    component                   TEXT NOT NULL,
    hour_bucket                 TIMESTAMPTZ NOT NULL,
    s3_key                      TEXT NOT NULL,

    n_attempts                  INTEGER NOT NULL DEFAULT 0,
    last_error                  TEXT,

    first_failed_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_failed_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- How many consecutive nightly archival jobs have failed for this bucket.
    -- When this reaches S3_ESCALATION_THRESHOLD a CRITICAL alert is written.
    consecutive_daily_failures  INTEGER NOT NULL DEFAULT 1,

    -- Set when the upload eventually succeeds on a retry run.
    -- Rows with resolved_at IS NOT NULL are excluded from future retries.
    resolved_at                 TIMESTAMPTZ,

    CONSTRAINT failed_uploads_bucket_unique
        UNIQUE (service, component, hour_bucket)
);

CREATE INDEX idx_failed_uploads_pending
    ON devlog.failed_uploads(service, component, hour_bucket)
    WHERE resolved_at IS NULL;

CREATE INDEX idx_failed_uploads_escalated
    ON devlog.failed_uploads(consecutive_daily_failures DESC)
    WHERE resolved_at IS NULL;

COMMENT ON TABLE devlog.failed_uploads IS
    'Tracks S3 upload failures across archival runs. '
    'Rows with resolved_at IS NULL will be retried on every subsequent run. '
    'consecutive_daily_failures drives escalation alerting — when it reaches '
    'S3_ESCALATION_THRESHOLD (3), a CRITICAL alert fires and hot storage '
    'accumulation is flagged to the on-call developer.';
-- Tracks each execution of the log archival process.
-- The archival job runs nightly (Airflow DAG) and:
--   1. Identifies execution_log rows older than 30 days
--   2. Computes hourly summaries and writes to execution_summaries
--   3. Compresses raw rows to JSONL.gz and uploads to S3
--   4. Deletes the raw rows from PostGIS (after S3 upload confirmed)
--   5. Writes an archive_jobs row recording what was done
-- =============================================================================
CREATE TABLE devlog.archive_jobs (
    job_id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    started_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at        TIMESTAMPTZ,
    status              TEXT NOT NULL DEFAULT 'running',
    -- 'running' | 'completed' | 'failed'

    -- What was archived
    archive_window_start    TIMESTAMPTZ NOT NULL,
    archive_window_end      TIMESTAMPTZ NOT NULL,

    n_rows_summarised       BIGINT,
    n_summaries_written     INTEGER,
    n_rows_archived_to_s3   BIGINT,
    n_rows_deleted_from_pg  BIGINT,

    -- S3 details
    s3_bucket               TEXT,
    s3_prefix               TEXT,
    total_compressed_mb     NUMERIC(12,3),

    -- Volume anomalies detected during this archive run
    n_volume_anomalies      INTEGER DEFAULT 0,

    error_message           TEXT,
    job_metadata            JSONB DEFAULT '{}'
);

-- =============================================================================
-- Helper functions
-- =============================================================================

-- Function: detect volume anomaly for a given service+component in a time window
-- Called by the archival job and by the real-time volume monitor
CREATE OR REPLACE FUNCTION devlog.detect_volume_anomaly(
    p_service       TEXT,
    p_component     TEXT,
    p_operation     TEXT,
    p_window_start  TIMESTAMPTZ,
    p_window_end    TIMESTAMPTZ
)
RETURNS TABLE (
    is_anomaly          BOOLEAN,
    n_logs_in_window    INTEGER,
    n_logs_expected     NUMERIC,
    volume_ratio        NUMERIC,
    severity            TEXT
)
LANGUAGE sql STABLE AS $$
    WITH
    -- Count logs in the alert window
    current_window AS (
        SELECT COUNT(*) AS n_current
        FROM devlog.execution_log
        WHERE service   = p_service
          AND component = p_component
          AND operation = p_operation
          AND logged_at BETWEEN p_window_start AND p_window_end
    ),
    -- Compute baseline: hourly average over the past 30 days
    -- excluding the current window to avoid contamination
    baseline AS (
        SELECT
            AVG(n_executions) AS avg_per_hour,
            STDDEV(n_executions) AS stddev_per_hour
        FROM devlog.execution_summaries
        WHERE service   = p_service
          AND component = p_component
          AND operation = p_operation
          AND hour_bucket >= NOW() - INTERVAL '30 days'
          AND hour_bucket < p_window_start
    ),
    window_hours AS (
        SELECT EXTRACT(EPOCH FROM (p_window_end - p_window_start)) / 3600 AS hours
    )
    SELECT
        -- Anomaly if actual > 5x expected
        (c.n_current > b.avg_per_hour * wh.hours * 5)::BOOLEAN AS is_anomaly,
        c.n_current::INTEGER,
        ROUND(b.avg_per_hour * wh.hours, 0),
        CASE
            WHEN b.avg_per_hour * wh.hours > 0
            THEN ROUND(c.n_current / (b.avg_per_hour * wh.hours), 2)
            ELSE NULL
        END AS volume_ratio,
        CASE
            WHEN c.n_current > b.avg_per_hour * wh.hours * 20 THEN 'CRITICAL'
            WHEN c.n_current > b.avg_per_hour * wh.hours * 5  THEN 'WARNING'
            WHEN c.n_current > b.avg_per_hour * wh.hours * 2  THEN 'NOTICE'
            ELSE 'NORMAL'
        END
    FROM current_window c, baseline b, window_hours wh;
$$;

-- Function: get a formatted developer log view (hides stack traces from non-devs)
-- The API layer calls this — returns stack_trace only when caller has dev role
CREATE OR REPLACE FUNCTION devlog.get_run_logs(
    p_run_id        UUID,
    p_include_debug BOOLEAN DEFAULT FALSE,
    p_include_stack BOOLEAN DEFAULT FALSE   -- requires developer role
)
RETURNS TABLE (
    logged_at       TIMESTAMPTZ,
    level           TEXT,
    component       TEXT,
    operation       TEXT,
    duration_ms     NUMERIC,
    rows_processed  INTEGER,
    error_type      TEXT,
    error_message   TEXT,
    stack_trace     TEXT,   -- NULL unless p_include_stack = TRUE
    context         JSONB
)
LANGUAGE sql STABLE AS $$
    SELECT
        logged_at,
        level,
        component,
        operation,
        duration_ms,
        rows_processed,
        error_type,
        error_message,
        CASE WHEN p_include_stack THEN stack_trace ELSE NULL END,
        context
    FROM devlog.execution_log
    WHERE run_id = p_run_id
      AND (p_include_debug OR level != 'DEBUG')
    ORDER BY logged_at;
$$;

COMMENT ON FUNCTION devlog.get_run_logs IS
    'Returns execution logs for an analysis run. '
    'stack_trace is only included when p_include_stack=TRUE (developer role required). '
    'Planners call this with p_include_stack=FALSE — they see timing and errors '
    'but never raw stack traces or internal implementation detail.';

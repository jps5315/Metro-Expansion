"""
engine/logging/structured_logger.py

Structured developer logging for the Metro Expansion Planner (MEP).

Design goals
------------
1.  Separation from planner audit logs
    This logger writes to devlog.execution_log — a completely separate schema
    from audit.event_log. Planners never see these records. Developers never
    need to dig through planner approval workflows to find a database timeout.

2.  Structured JSON — queryable logs
    Every log entry is a structured record with consistent typed fields:
    run_id, city_id, component, operation, duration_ms, error_code, context.
    This means logs can be queried: "show all gravity_catchment runs that
    took > 30 seconds in the last 7 days" rather than grepping text files.

3.  Context propagation
    The MEPLogger instance carries a context dict (run_id, city_id, etc.)
    that is automatically included in every log entry. Callers set context
    once at the start of a pipeline and every subsequent log is correlated.

4.  Performance instrumentation via context manager
    The .time_operation() context manager records duration, memory delta,
    and row throughput for any code block without cluttering business logic.

5.  Volume protection
    The logger monitors its own write rate. If a single component exceeds
    MAX_LOGS_PER_MINUTE it activates sampling — dropping DEBUG and INFO
    entries while preserving WARNING, ERROR, CRITICAL. This prevents a
    runaway loop from generating unbounded storage costs.

6.  Tiered output
    - Always: writes structured records to devlog.execution_log (PostGIS hot tier)
    - Always: emits to Python's standard logging (captured by deployment platform)
    - In development: pretty-prints to console
    - In production: Python logging captured by Fluentd → CloudWatch/Datadog

Usage
-----
    from engine.logging.structured_logger import MEPLogger

    # Instantiate once per pipeline execution
    logger = MEPLogger(
        db_url="postgresql://...",
        service="suitability_engine",
        component="HospitalSuitabilityAnalyzer",
        run_id="uuid-here",
        city_id="uuid-here"
    )

    # Simple log
    logger.info("load_layers", "Starting data load", context={"n_layers": 9})

    # Timed block — automatically records duration, memory, rows
    with logger.time_operation("_score_gravity_catchment") as timer:
        result = self._score_gravity_catchment(centroids, parcel_index)
        timer.rows_processed = len(parcel_index)

    # Error with exception
    try:
        ...
    except Exception as e:
        logger.error("apply_hard_constraints", "Flood zone query failed",
                     exc=e, context={"parcel_count": len(parcels)})
        raise

Author: MEP Team
"""

from __future__ import annotations

import logging
import os
import resource
import sys
import time
import traceback
import uuid
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Generator, Optional

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool

# Standard Python logger — output captured by deployment platform (Fluentd/CloudWatch)
_stdlib_logger = logging.getLogger("mep.engine")

# ---------------------------------------------------------------------------
# Volume protection constants
# ---------------------------------------------------------------------------

# If a single component writes more than this many logs per minute,
# activate sampling to protect storage
MAX_LOGS_PER_MINUTE = 500

# Sampling rates activated when volume protection kicks in
# Higher-severity levels are NEVER sampled — only debug/info
SAMPLING_RATES = {
    "DEBUG":    100,   # keep 1 in 100
    "INFO":     10,    # keep 1 in 10
    "WARNING":  1,     # keep all
    "ERROR":    1,     # keep all
    "CRITICAL": 1,     # keep all
}

# How many recent timestamps to track for rate calculation
RATE_WINDOW_SIZE = 1000


# ---------------------------------------------------------------------------
# Timer context manager return object
# ---------------------------------------------------------------------------

@dataclass
class OperationTimer:
    """
    Returned by logger.time_operation(). Caller can set rows_processed
    inside the with-block to get throughput metrics.
    """
    operation:      str
    rows_processed: int = 0
    extra_context:  dict = field(default_factory=dict)
    _start_time:    float = field(default_factory=time.perf_counter, init=False)
    _start_mem:     int   = field(default_factory=lambda: resource.getrusage(
                                resource.RUSAGE_SELF).ru_maxrss, init=False)


# ---------------------------------------------------------------------------
# Log record — the unit of work passed to the writer
# ---------------------------------------------------------------------------

@dataclass
class LogRecord:
    service:         str
    component:       str
    operation:       str
    level:           str
    message:         str
    logged_at:       datetime
    run_id:          Optional[str]  = None
    city_id:         Optional[str]  = None
    scenario_id:     Optional[str]  = None
    request_id:      Optional[str]  = None
    worker_id:       Optional[str]  = None
    trace_id:        Optional[str]  = None
    duration_ms:     Optional[float] = None
    cpu_ms:          Optional[float] = None
    memory_mb:       Optional[float] = None
    rows_processed:  Optional[int]   = None
    rows_per_second: Optional[float] = None
    error_type:      Optional[str]   = None
    error_message:   Optional[str]   = None
    stack_trace:     Optional[str]   = None
    error_code:      Optional[str]   = None
    context:         dict            = field(default_factory=dict)
    host_name:       str             = field(default_factory=lambda: os.uname().nodename
                                             if hasattr(os, 'uname') else "unknown")
    pod_name:        str             = field(default_factory=lambda: os.environ.get(
                                             "HOSTNAME", ""))
    git_commit:      str             = field(default_factory=lambda: os.environ.get(
                                             "GIT_COMMIT", ""))
    app_version:     str             = field(default_factory=lambda: os.environ.get(
                                             "APP_VERSION", ""))


# ---------------------------------------------------------------------------
# Main logger class
# ---------------------------------------------------------------------------

class MEPLogger:
    """
    Structured developer logger for MEP pipeline components.

    Thread-safety: one instance per pipeline run. Do not share across threads.
    For multi-threaded/multi-process (Celery workers, Spark executors),
    each worker creates its own MEPLogger instance.
    """

    def __init__(
        self,
        db_url:      str,
        service:     str,
        component:   str,
        run_id:      Optional[str] = None,
        city_id:     Optional[str] = None,
        scenario_id: Optional[str] = None,
        request_id:  Optional[str] = None,
        worker_id:   Optional[str] = None,
        min_level:   str           = "INFO",
        enable_db_write: bool      = True,
        enable_console:  bool      = False,  # True in dev, False in production
    ) -> None:
        self.db_url      = db_url
        self.service     = service
        self.component   = component
        self.run_id      = run_id
        self.city_id     = city_id
        self.scenario_id = scenario_id
        self.request_id  = request_id
        self.worker_id   = worker_id or os.environ.get("CELERY_WORKER_ID", "")
        self.min_level   = min_level
        self.enable_db_write  = enable_db_write
        self.enable_console   = enable_console

        # Volume protection state
        self._recent_timestamps: deque = deque(maxlen=RATE_WINDOW_SIZE)
        self._sampling_active   = False
        self._sampling_counters: dict[str, int] = {}
        self._volume_alert_sent = False

        # DB engine — NullPool because we write in short bursts, not sustained connections
        self._engine: Optional[Engine] = None
        if enable_db_write:
            self._engine = create_engine(db_url, poolclass=NullPool)

        # Batch buffer — accumulate records and flush every N entries or on error
        # Reduces DB round-trips from one-per-log to one-per-batch
        self._batch:      list[LogRecord] = []
        self._batch_size: int             = 50
        self._flush_on_levels             = {"ERROR", "CRITICAL"}

        # Level ordering for filtering
        self._level_order = {"DEBUG": 0, "INFO": 1, "WARNING": 2,
                             "ERROR": 3, "CRITICAL": 4}

    # ==========================================================================
    # Public logging methods
    # ==========================================================================

    def debug(self, operation: str, message: str, **kwargs) -> None:
        self._log("DEBUG", operation, message, **kwargs)

    def info(self, operation: str, message: str, **kwargs) -> None:
        self._log("INFO", operation, message, **kwargs)

    def warning(self, operation: str, message: str, **kwargs) -> None:
        self._log("WARNING", operation, message, **kwargs)

    def error(
        self,
        operation: str,
        message: str,
        exc: Optional[Exception] = None,
        error_code: Optional[str] = None,
        **kwargs
    ) -> None:
        self._log("ERROR", operation, message, exc=exc,
                  error_code=error_code, **kwargs)

    def critical(
        self,
        operation: str,
        message: str,
        exc: Optional[Exception] = None,
        error_code: Optional[str] = None,
        **kwargs
    ) -> None:
        self._log("CRITICAL", operation, message, exc=exc,
                  error_code=error_code, **kwargs)

    # ==========================================================================
    # Timed operation context manager
    # ==========================================================================

    @contextmanager
    def time_operation(
        self,
        operation: str,
        context: Optional[dict] = None,
        log_start: bool = True,
    ) -> Generator[OperationTimer, None, None]:
        """
        Context manager that automatically logs duration, memory delta,
        and row throughput for a code block.

        Usage:
            with logger.time_operation("_score_gravity_catchment",
                                       context={"beta": 1.8}) as t:
                result = self._score_gravity_catchment(centroids, index)
                t.rows_processed = len(index)
            # On exit: INFO log written with duration_ms, memory_mb, rows/sec

        On exception: ERROR log written with stack trace, then exception re-raised.
        """
        timer = OperationTimer(
            operation=operation,
            extra_context=context or {}
        )

        if log_start:
            self.debug(operation, f"Starting {operation}",
                       context={"phase": "start", **(context or {})})

        try:
            yield timer

        except Exception as exc:
            # Measure up to the point of failure
            wall_ms  = (time.perf_counter() - timer._start_time) * 1000
            mem_now  = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            mem_delta = (mem_now - timer._start_mem) / 1024  # bytes → MB on Linux

            self._log(
                "ERROR", operation,
                f"{operation} failed after {wall_ms:.0f}ms",
                exc=exc,
                duration_ms=wall_ms,
                memory_mb=mem_delta,
                rows_processed=timer.rows_processed,
                context={
                    "phase": "failed",
                    **timer.extra_context,
                    **getattr(timer, "extra_context", {}),
                }
            )
            raise  # always re-raise — we log but never suppress

        else:
            # Success path
            wall_ms  = (time.perf_counter() - timer._start_time) * 1000
            mem_now  = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            mem_delta = (mem_now - timer._start_mem) / 1024

            rps = None
            if timer.rows_processed > 0 and wall_ms > 0:
                rps = timer.rows_processed / (wall_ms / 1000)

            ctx = {
                "phase": "completed",
                **timer.extra_context,
            }
            if timer.rows_processed > 0:
                ctx["rows_processed"] = timer.rows_processed

            self._log(
                "INFO", operation,
                f"{operation} completed in {wall_ms:.0f}ms"
                + (f" | {timer.rows_processed:,} rows"
                   f" | {rps:,.0f} rows/sec" if rps else ""),
                duration_ms=wall_ms,
                memory_mb=mem_delta,
                rows_processed=timer.rows_processed,
                rows_per_second=rps,
                context=ctx
            )

    # ==========================================================================
    # Context update — call when run_id or city_id becomes known mid-pipeline
    # ==========================================================================

    def set_context(self, **kwargs) -> None:
        """
        Update correlation identifiers. Useful when run_id is not known
        at logger construction time (e.g. it is generated after DB insert).

        logger.set_context(run_id="uuid-here", city_id="uuid-here")
        """
        for key in ("run_id", "city_id", "scenario_id", "request_id"):
            if key in kwargs:
                setattr(self, key, kwargs[key])

    def flush(self) -> None:
        """Force-write any buffered log records to the database."""
        if self._batch:
            self._write_batch(self._batch)
            self._batch.clear()

    def __del__(self) -> None:
        """Flush on garbage collection."""
        try:
            self.flush()
        except Exception:
            pass

    # ==========================================================================
    # Private implementation
    # ==========================================================================

    def _log(
        self,
        level:          str,
        operation:      str,
        message:        str,
        exc:            Optional[Exception] = None,
        error_code:     Optional[str]       = None,
        duration_ms:    Optional[float]     = None,
        memory_mb:      Optional[float]     = None,
        rows_processed: Optional[int]       = None,
        rows_per_second: Optional[float]    = None,
        context:        Optional[dict]      = None,
    ) -> None:
        """Core internal log method."""

        # Level filter
        if self._level_order.get(level, 0) < self._level_order.get(self.min_level, 1):
            return

        # Volume protection sampling
        if not self._check_volume_and_sample(level):
            return

        # Build exception details
        error_type    = None
        error_message = None
        stack_trace   = None

        if exc is not None:
            error_type    = type(exc).__name__
            error_message = str(exc)
            stack_trace   = traceback.format_exc()

        record = LogRecord(
            service        = self.service,
            component      = self.component,
            operation      = operation,
            level          = level,
            message        = message,
            logged_at      = datetime.now(timezone.utc),
            run_id         = self.run_id,
            city_id        = self.city_id,
            scenario_id    = self.scenario_id,
            request_id     = self.request_id,
            worker_id      = self.worker_id,
            duration_ms    = duration_ms,
            memory_mb      = memory_mb,
            rows_processed = rows_processed,
            rows_per_second= rows_per_second,
            error_type     = error_type,
            error_message  = error_message,
            stack_trace    = stack_trace,
            error_code     = error_code,
            context        = context or {},
        )

        # Emit to standard Python logging (captured by platform)
        self._emit_stdlib(record)

        # Console output (development only)
        if self.enable_console:
            self._emit_console(record)

        # Buffer for DB write
        if self.enable_db_write:
            self._batch.append(record)

            # Flush immediately on errors, otherwise wait for batch
            if level in self._flush_on_levels or len(self._batch) >= self._batch_size:
                self.flush()

    def _check_volume_and_sample(self, level: str) -> bool:
        """
        Check current log rate and apply sampling if rate exceeds threshold.

        Returns True if this record should be written, False if sampled out.
        WARNING, ERROR, CRITICAL are never sampled — only DEBUG and INFO.
        """
        # Never sample high-severity levels
        if level in ("WARNING", "ERROR", "CRITICAL"):
            self._recent_timestamps.append(time.monotonic())
            return True

        # Track recent write timestamps
        now = time.monotonic()
        self._recent_timestamps.append(now)

        # Calculate current rate over the last 60 seconds
        cutoff = now - 60.0
        recent_count = sum(1 for t in self._recent_timestamps if t > cutoff)
        current_rate = recent_count  # logs per minute

        if current_rate > MAX_LOGS_PER_MINUTE:
            if not self._sampling_active:
                self._sampling_active = True
                # Log the activation itself at WARNING so it is not sampled out
                _stdlib_logger.warning(
                    "MEPLogger volume protection activated for component=%s. "
                    "Rate: %d logs/min (threshold: %d). "
                    "DEBUG/INFO will be sampled. "
                    "Check for runaway loops.",
                    self.component, current_rate, MAX_LOGS_PER_MINUTE
                )
                # Write volume alert to DB (will be flushed with next error or batch)
                self._write_volume_alert(current_rate)

            # Apply sampling
            sample_rate = SAMPLING_RATES.get(level, 1)
            if sample_rate > 1:
                counter = self._sampling_counters.get(level, 0)
                self._sampling_counters[level] = counter + 1
                if counter % sample_rate != 0:
                    return False  # drop this record
        else:
            # Rate is normal — deactivate sampling if it was active
            if self._sampling_active:
                self._sampling_active = False
                _stdlib_logger.info(
                    "MEPLogger volume protection deactivated for component=%s. "
                    "Rate normalised to %d logs/min.",
                    self.component, current_rate
                )

        return True

    def _write_volume_alert(self, current_rate: int) -> None:
        """Write a volume alert record to devlog.log_volume_alerts."""
        if self._engine is None or self._volume_alert_sent:
            return
        try:
            with self._engine.begin() as conn:
                conn.execute(text("""
                    INSERT INTO devlog.log_volume_alerts (
                        service, component, operation,
                        window_start, window_end, window_minutes,
                        n_logs_in_window, volume_ratio,
                        severity, throttle_applied, throttle_action
                    ) VALUES (
                        :service, :component, NULL,
                        NOW() - INTERVAL '1 minute', NOW(), 1,
                        :n_logs, :ratio,
                        CASE
                            WHEN :n_logs > :threshold * 20 THEN 'CRITICAL'
                            WHEN :n_logs > :threshold * 5  THEN 'WARNING'
                            ELSE 'NOTICE'
                        END,
                        TRUE,
                        CASE
                            WHEN :n_logs > :threshold * 20 THEN 'SAMPLE_1_IN_1000'
                            WHEN :n_logs > :threshold * 5  THEN 'SAMPLE_1_IN_100'
                            ELSE 'DROP_DEBUG'
                        END
                    )
                """), {
                    "service":   self.service,
                    "component": self.component,
                    "n_logs":    current_rate,
                    "ratio":     round(current_rate / MAX_LOGS_PER_MINUTE, 2),
                    "threshold": MAX_LOGS_PER_MINUTE,
                })
            self._volume_alert_sent = True
        except Exception as e:
            _stdlib_logger.error("Failed to write volume alert: %s", e)

    def _write_batch(self, batch: list[LogRecord]) -> None:
        """Bulk-insert a batch of log records into devlog.execution_log."""
        if not batch or self._engine is None:
            return

        rows = [
            {
                "service":          r.service,
                "component":        r.component,
                "operation":        r.operation,
                "level":            r.level,
                "run_id":           r.run_id,
                "city_id":          r.city_id,
                "scenario_id":      r.scenario_id,
                "request_id":       r.request_id,
                "worker_id":        r.worker_id,
                "trace_id":         r.trace_id,
                "span_id":          getattr(r, "span_id", None),
                "duration_ms":      r.duration_ms,
                "cpu_ms":           r.cpu_ms,
                "memory_mb":        r.memory_mb,
                "rows_processed":   r.rows_processed,
                "rows_per_second":  r.rows_per_second,
                "error_type":       r.error_type,
                "error_message":    r.error_message,
                "stack_trace":      r.stack_trace,
                "error_code":       r.error_code,
                "context":          r.context,
                "host_name":        r.host_name,
                "pod_name":         r.pod_name,
                "git_commit":       r.git_commit,
                "app_version":      r.app_version,
                "logged_at":        r.logged_at,
            }
            for r in batch
        ]

        try:
            with self._engine.begin() as conn:
                conn.execute(text("""
                    INSERT INTO devlog.execution_log (
                        service, component, operation, level,
                        run_id, city_id, scenario_id, request_id, worker_id,
                        trace_id, span_id,
                        duration_ms, cpu_ms, memory_mb,
                        rows_processed, rows_per_second,
                        error_type, error_message, stack_trace, error_code,
                        context, host_name, pod_name, git_commit, app_version,
                        logged_at
                    ) VALUES (
                        :service, :component, :operation, :level,
                        :run_id::UUID, :city_id::UUID, :scenario_id::UUID,
                        :request_id, :worker_id,
                        :trace_id, :span_id,
                        :duration_ms, :cpu_ms, :memory_mb,
                        :rows_processed, :rows_per_second,
                        :error_type, :error_message, :stack_trace, :error_code,
                        :context::JSONB, :host_name, :pod_name,
                        :git_commit, :app_version,
                        :logged_at
                    )
                """), rows)
        except Exception as e:
            # Never let logging failure crash the main application
            # Fall back to stdlib logger only
            _stdlib_logger.error(
                "MEPLogger failed to write %d records to devlog.execution_log: %s",
                len(batch), e
            )

    def _emit_stdlib(self, record: LogRecord) -> None:
        """Emit to Python's standard logging for platform capture."""
        level_map = {
            "DEBUG":    logging.DEBUG,
            "INFO":     logging.INFO,
            "WARNING":  logging.WARNING,
            "ERROR":    logging.ERROR,
            "CRITICAL": logging.CRITICAL,
        }
        level_int = level_map.get(record.level, logging.INFO)
        _stdlib_logger.log(
            level_int,
            "[%s.%s] %s | run=%s | city=%s | duration=%sms",
            record.component,
            record.operation,
            record.message,
            record.run_id or "-",
            record.city_id or "-",
            f"{record.duration_ms:.0f}" if record.duration_ms else "-",
        )

    def _emit_console(self, record: LogRecord) -> None:
        """Pretty-print for local development."""
        COLOURS = {
            "DEBUG":    "\033[37m",    # grey
            "INFO":     "\033[32m",    # green
            "WARNING":  "\033[33m",    # yellow
            "ERROR":    "\033[31m",    # red
            "CRITICAL": "\033[35m",    # magenta
        }
        RESET = "\033[0m"
        colour = COLOURS.get(record.level, "")

        timing = f" [{record.duration_ms:.0f}ms]" if record.duration_ms else ""
        rows   = f" {record.rows_processed:,}rows" if record.rows_processed else ""

        print(
            f"{colour}[{record.level}]{RESET} "
            f"{record.logged_at.strftime('%H:%M:%S')} "
            f"{record.component}.{record.operation}{timing}{rows} "
            f"— {record.message}",
            file=sys.stderr
        )
        if record.error_type:
            print(f"  {record.error_type}: {record.error_message}", file=sys.stderr)

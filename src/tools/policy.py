from datetime import UTC, datetime, timedelta

# Read as policy.<NAME> at call time so tests can tighten them with monkeypatch.
MAX_TOOL_CALLS_PER_ASSIGNMENT = 40
MAX_REJECTIONS_PER_ASSIGNMENT = 20
MAX_CONCURRENT_PER_ASSIGNMENT = 3
# Covers the primary query, its retry and the coverage probe. Above the adapter's 15 s
# HTTP timeout so a single slow attempt surfaces as signoz_timeout, not executor.timeout.
EXECUTION_DEADLINE_SECONDS = 20.0
MAX_RETRIES = 1
RETRY_BACKOFF_SECONDS = 0.5
# A probe that cannot finish says nothing about coverage and only delays the result.
MIN_PROBE_SECONDS = 2.0
MAX_LOOKBACK = timedelta(days=7)
# Bounds each short phase-1/phase-3 transaction, including waits on the assignment lock.
DB_TIMEOUT_SECONDS = 10.0
# A running row older than this has outlived any deadline: its executor died.
STALE_AFTER = timedelta(seconds=2 * EXECUTION_DEADLINE_SECONDS)


class Rejection(Exception):
    """A request the executor refuses before any telemetry query. Message is sanitized."""

    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.retryable = retryable


def check_lookback(start: datetime) -> None:
    if start < datetime.now(UTC) - MAX_LOOKBACK:
        raise Rejection(
            "policy.lookback_exceeded", f"time_range.start may be at most {MAX_LOOKBACK} ago"
        )

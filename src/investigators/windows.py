"""Explicit time windows in questions, and whether evidence covers them.

Deterministic, deliberately narrow: "last/past N minutes|hours|days", "last hour",
"right now"/"currently". A question without such a phrase states no window, and the
model judges sufficiency alone. When it does state one, memory only counts as covering
it if some evidence window spans it, so "is it still failing right now?" is never
answered from evidence collected an hour ago.
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from src.tools.policy import MAX_LOOKBACK

UNITS = {"minute": 60, "min": 60, "hour": 3600, "hr": 3600, "day": 86400}
_SPAN = re.compile(
    r"\b(?:last|past|previous)\s+(\d+|an?|one)?\s*(minute|min|hour|hr|day)s?\b", re.IGNORECASE
)
_NOW = re.compile(r"\b(?:right now|currently|at the moment|still\b.*\bnow)\b", re.IGNORECASE)
# What "now" means when a question asks about the current state.
NOW_WINDOW = timedelta(minutes=15)
# Evidence windows are chosen by models; a few minutes either side still covers.
COVERAGE_SLACK = timedelta(minutes=5)


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime
    label: str

    def text(self) -> str:
        return f"{self.label} ({self.start:%Y-%m-%dT%H:%M}Z to {self.end:%Y-%m-%dT%H:%M}Z)"


def requested_window(question: str, now: datetime | None = None) -> Window | None:
    now = now or datetime.now(UTC)
    match = _SPAN.search(question)
    if match:
        amount = match.group(1)
        n = 1 if amount is None or not amount.isdigit() else int(amount)
        seconds = n * UNITS[match.group(2).lower()]
        span = min(timedelta(seconds=seconds), MAX_LOOKBACK)
        return Window(now - span, now, match.group(0).strip())
    if _NOW.search(question):
        return Window(now - NOW_WINDOW, now, "now")
    return None


def parse_range(time_range: object) -> tuple[datetime, datetime] | None:
    """An evidence data_json time_range ({"start", "end"} ISO strings), if well-formed."""
    if not isinstance(time_range, dict):
        return None
    try:
        start = datetime.fromisoformat(str(time_range["start"]).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(time_range["end"]).replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return None
    if start.tzinfo is None or end.tzinfo is None:
        return None
    return start, end


def covers(window: Window, evidence: list[tuple[datetime, datetime]]) -> bool:
    return any(
        start <= window.start + COVERAGE_SLACK and end >= window.end - COVERAGE_SLACK
        for start, end in evidence
    )

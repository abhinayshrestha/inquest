from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base


class RateLimitCounter(Base):
    """Requests counted in one fixed window for one limited subject (D039). Shared by every
    application process; incremented with one atomic upsert. `bucket` is the SHA-256 of the
    limit's name and its subject (an address, user or organization id), so the table holds
    no addresses. Rows are deleted once their window has passed."""

    __tablename__ = "rate_limit_counters"

    bucket: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    hits: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

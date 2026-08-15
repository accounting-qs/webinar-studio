"""Durable per-webinar report generation requests.

A row = "this webinar needs a report generated". Written on every Generate
click / scheduler prep, deleted when generation completes. The scheduler's
report sweep retries any surviving row, so a deploy or crash mid-generation
no longer loses the request. See services.webinar_report.
"""

from db.models._common import (
    Base, DateTime, Integer, Mapped, Optional, Text,
    datetime, func, mapped_column,
)


class WebinarReportRequest(Base):
    __tablename__ = "webinar_report_request"

    webinar_id: Mapped[str] = mapped_column(Text, primary_key=True)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[Optional[str]] = mapped_column(Text)

"""Persisted per-webinar reports.

Each row is a frozen report artifact for one webinar variant: the computed
numbers payload (scorecard vs baselines, per-dimension funnels, bookings
deep-dive, non-joiner package, caveats) plus the AI-generated insights that
render at the bottom of the report page and inside the weekly email. Built by
services.webinar_report.generate_report(); the page/email only read this table.
"""

from db.models._common import (
    Base, DateTime, Integer, JSONB, Mapped, Optional, Text,
    datetime, func, mapped_column,
)


class WebinarReport(Base):
    __tablename__ = "webinar_report"

    webinar_id: Mapped[str] = mapped_column(Text, primary_key=True)
    webinar_number: Mapped[Optional[int]] = mapped_column(Integer)
    variant_label: Mapped[Optional[str]] = mapped_column(Text)
    # Full report numbers (scorecard, funnels, bookings, nonjoiners, caveats).
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # AI insights: [{"title": str, "bullets": [str, ...]}, ...] — null if the
    # AI step failed (report still valid; ai_error carries the reason).
    insights: Mapped[Optional[list]] = mapped_column(JSONB)
    insights_model: Mapped[Optional[str]] = mapped_column(Text)
    ai_error: Mapped[Optional[str]] = mapped_column(Text)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    generation_ms: Mapped[Optional[int]] = mapped_column(Integer)

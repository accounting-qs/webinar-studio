"""Persisted statistics snapshots.

Each row holds the fully-computed per-webinar statistics payload (the heavy
output of services.statistics.get_statistics_webinar_one) so the dashboard can
read it back instantly instead of recomputing the ~30s contacts↔ghl_contact
join on every page load. Rows are rebuilt by services.statistics_snapshot
.recompute() whenever a source data entry changes (GHL sync, WebinarGeek sync,
calendar upload, webinar edit) or on a manual trigger.
"""

from db.models._common import (
    Base, DateTime, Integer, JSONB, Mapped, Optional, String, Text,
    datetime, func, mapped_column,
)


class StatisticsSnapshot(Base):
    __tablename__ = "statistics_snapshot"

    # (source, webinar_id) is the natural key: each webinar-variant (its own
    # UUID) has one snapshot per source ("ghl" | "workbook").
    source: Mapped[str] = mapped_column(String(20), primary_key=True)
    webinar_id: Mapped[str] = mapped_column(Text, primary_key=True)
    webinar_number: Mapped[Optional[int]] = mapped_column(Integer)
    variant_label: Mapped[Optional[str]] = mapped_column(Text)
    # Full processed webinar dict (id, summary, rows[…], etc.).
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

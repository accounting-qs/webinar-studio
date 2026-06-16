"""SOP video model: editable how-it-works walkthroughs shown on the SOP page.

Each row is one walkthrough card — a title, a short subtitle, and a Loom
share URL embedded on the page. Rows are seeded by migration 049 and edited
in-app (title/subtitle/link); the set is fixed (no add/delete from the UI).
"""

from db.models._common import (
    Base, DateTime, Integer, Mapped, Text, UUID, datetime, func, gen_uuid, mapped_column,
)


class SopVideo(Base):
    __tablename__ = "sop_videos"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    subtitle: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    loom_url: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    # Lower numbers sort first on the page.
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

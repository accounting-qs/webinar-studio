"""Weekly report settings (singleton row, id=1)."""
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class ReportSettings(Base):
    __tablename__ = "report_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # always 1 (singleton)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    day_of_week: Mapped[str] = mapped_column(String(3), nullable=False, server_default="wed")
    hour_local: Mapped[int] = mapped_column(Integer, nullable=False, server_default="14")
    minute_local: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    timezone: Mapped[str] = mapped_column(Text, nullable=False, server_default="America/Chicago")
    recipients: Mapped[list] = mapped_column(JSONB, nullable=False, server_default='["geri@quantum-scaling.com"]')
    from_address: Mapped[str] = mapped_column(Text, nullable=False, server_default="reports@qs-institutes.com")
    last_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

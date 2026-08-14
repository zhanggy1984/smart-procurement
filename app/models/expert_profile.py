"""专家画像（P3.5 归档重算 / P4.2 排序用）。对应 P0.4 migration。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import JSON, DateTime, Integer, Numeric, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ExpertProfile(Base):
    """专家画像：评审量、返评率、评分质量（P4.2 加权排序 review_quality 来源）。"""

    __tablename__ = "expert_profile"

    expert_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    total_reviews: Mapped[int] = mapped_column(Integer, server_default="0")
    avg_return_rate: Mapped[Optional[Decimal]] = mapped_column(Numeric(4, 3), server_default="0")
    avg_reasoning_score: Mapped[Optional[Decimal]] = mapped_column(Numeric(4, 3), server_default="0.7")
    review_quality: Mapped[Optional[Decimal]] = mapped_column(Numeric(4, 3), server_default="0.7")
    dimension_stats: Mapped[Optional[dict]] = mapped_column(JSON)
    calibration_status: Mapped[str] = mapped_column(String(16), server_default="UNCALIBRATED")
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, server_default=text("NOW()"))

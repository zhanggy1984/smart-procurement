"""评审记录模型（P1.4 起，黑名单级联需操作该表；P1.7 完善评审流）。

对应 P0.4 migration 建的表。status: DRAFT→CONFIRMED/MANUAL_ADJUSTED（整本提交后锁定），
SUSPENDED 为供应商拉黑级联触发，previous_status 存 SUSPENDED 前原状态快照，
解除拉黑后按快照恢复（solution.md 1.2）。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import JSON, DateTime, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ReviewStatus:
    """评审记录状态。"""

    DRAFT = "DRAFT"
    CONFIRMED = "CONFIRMED"
    MANUAL_ADJUSTED = "MANUAL_ADJUSTED"
    SUSPENDED = "SUSPENDED"

    ALL = (DRAFT, CONFIRMED, MANUAL_ADJUSTED, SUSPENDED)


class ExpertReview(Base):
    """专家对某标书某维度的评审记录。"""

    __tablename__ = "expert_review"

    review_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    expert_id: Mapped[str] = mapped_column(String(64), nullable=False)
    bid_id: Mapped[str] = mapped_column(String(64), nullable=False)
    dimension_id: Mapped[str] = mapped_column(String(64), nullable=False)
    score: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 2))
    comment: Mapped[Optional[str]] = mapped_column(Text)
    ai_suggestion: Mapped[Optional[dict]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), server_default=ReviewStatus.DRAFT)
    previous_status: Mapped[Optional[str]] = mapped_column(String(16))
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

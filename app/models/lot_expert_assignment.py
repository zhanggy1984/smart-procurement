"""专家-标段分配（P4.2 落库 / P4.3 申报）。对应 P0.4 migration。"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, BigInteger, DateTime, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AssignmentStatus:
    """分配状态（P4.3 申报流转）。"""

    PENDING_DECLARATION = "PENDING_DECLARATION"
    IN_PROGRESS = "IN_PROGRESS"
    CONFLICT_DECLARED = "CONFLICT_DECLARED"


class LotExpertAssignment(Base):
    """标段→专家分配（含维度分配）。同标段专家唯一。"""

    __tablename__ = "lot_expert_assignment"
    __table_args__ = (UniqueConstraint("lot_id", "expert_id", name="uq_lot_expert"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    lot_id: Mapped[str] = mapped_column(String(64), nullable=False)
    expert_id: Mapped[str] = mapped_column(String(64), nullable=False)
    dimension_ids: Mapped[Optional[list]] = mapped_column(JSON)
    match_batch_id: Mapped[Optional[str]] = mapped_column(String(64))
    assigned_at: Mapped[Optional[datetime]] = mapped_column(DateTime, server_default=text("NOW()"))
    status: Mapped[str] = mapped_column(String(32), server_default=AssignmentStatus.PENDING_DECLARATION)

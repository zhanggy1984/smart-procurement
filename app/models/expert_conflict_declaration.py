"""专家回避申报记录（P4.3）。对应 P0.4 migration。"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, DateTime, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ExpertConflictDeclaration(Base):
    """专家申报的回避关系（逐供应商）。申报冲突时写入并同步 Neo4j。"""

    __tablename__ = "expert_conflict_declaration"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    assignment_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expert_id: Mapped[str] = mapped_column(String(64), nullable=False)
    lot_id: Mapped[str] = mapped_column(String(64), nullable=False)
    supplier_id: Mapped[Optional[str]] = mapped_column(String(64))
    relation_type: Mapped[str] = mapped_column(String(32), nullable=False)
    relation_detail: Mapped[Optional[str]] = mapped_column(Text)
    declared_at: Mapped[Optional[datetime]] = mapped_column(DateTime, server_default=text("NOW()"))

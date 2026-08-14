"""企查查冷数据模型（P1.4）：pending_conflict。

对应 P0.4 migration 建的表。语义（solution.md 1.3）：
企查查 CSV 导入时"人匹配到专家但企业未匹配到供应商"的关系暂存于此，
供应商入库（P1.4 import 或唤醒）时按 credit_code/企业名回填 supplier_id，
status PENDING→ACTIVATED，并补写 Neo4j 关系。
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, DateTime, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class PendingConflictStatus:
    """冷数据处理状态。"""

    PENDING = "PENDING"
    ACTIVATED = "ACTIVATED"


class PendingConflict(Base):
    """企查查冷数据：人已匹配、企业未匹配的回避关系暂存。"""

    __tablename__ = "pending_conflict"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    person_name: Mapped[Optional[str]] = mapped_column(String(64))
    company_name: Mapped[Optional[str]] = mapped_column(String(256))
    credit_code: Mapped[Optional[str]] = mapped_column(String(32))
    relation_type: Mapped[Optional[str]] = mapped_column(String(32))
    expert_id: Mapped[Optional[str]] = mapped_column(String(64))
    supplier_id: Mapped[Optional[str]] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), server_default=PendingConflictStatus.PENDING)
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime, server_default=text("NOW()"))

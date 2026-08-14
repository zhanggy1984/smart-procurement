"""投标文件模型（P1.4 起，黑名单级联需操作该表；P1.5 完善上传/解析）。

对应 P0.4 migration 建的表。status: SUBMITTED→FROZEN（P1.5 封存）→...，
DISQUALIFIED 为供应商拉黑级联触发（freeze_hash 非空 = 已封存，不可废标；
未封存即 freeze_hash IS NULL 的标书在拉黑时废标）。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import JSON, DateTime, Integer, Numeric, SmallInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class BidStatus:
    """投标文件状态机。

    SUBMITTED（已上传）→ PARSING（P2.1 解析中）→ PARSED（解析完成）
    → FROZEN（围串标初筛通过封存，不可改）；解析失败 → PARSE_FAILED（可重试）。
    DISQUALIFIED 为供应商拉黑级联触发（freeze_hash 非空 = 已封存，不可废标；
    未封存即 freeze_hash IS NULL 的标书在拉黑时废标）。P1.5 只用到 SUBMITTED，
    PARSING/PARSED/PARSE_FAILED 由 P2.1 解析流水线驱动。
    """

    SUBMITTED = "SUBMITTED"
    PARSING = "PARSING"
    PARSED = "PARSED"
    PARSE_FAILED = "PARSE_FAILED"
    FROZEN = "FROZEN"
    DISQUALIFIED = "DISQUALIFIED"

    ALL = (SUBMITTED, PARSING, PARSED, PARSE_FAILED, FROZEN, DISQUALIFIED)


class BidDocument(Base):
    """投标文件记录。freeze_hash 为封存数据哈希（P1.5 冻结时写入）。"""

    __tablename__ = "bid_document"

    bid_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    lot_id: Mapped[str] = mapped_column(String(64), nullable=False)
    supplier_id: Mapped[str] = mapped_column(String(64), nullable=False)
    bid_amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(15, 2))
    duration: Mapped[Optional[int]] = mapped_column(Integer)
    team_size: Mapped[Optional[int]] = mapped_column(Integer)
    structured_data: Mapped[Optional[dict]] = mapped_column(JSON)
    file_url: Mapped[Optional[str]] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(16), server_default=BidStatus.SUBMITTED)
    freeze_hash: Mapped[Optional[str]] = mapped_column(String(128))
    parsing_step: Mapped[Optional[int]] = mapped_column(SmallInteger, server_default="0")
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

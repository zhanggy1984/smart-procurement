"""Outbox 事件模型（P1.3 写入 / P1.6 消费）。

对应 P0.4 migration 的 outbox_event 表。事件类型常量集中定义，
供写入方（service）与消费方（worker）共用，避免字符串拼写漂移。
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, BigInteger, DateTime, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class OutboxEventType:
    """业务事件类型（MySQL → Neo4j/Milvus 同步触发）。"""

    PROJECT_CREATED = "PROJECT_CREATED"
    LOT_CREATED = "LOT_CREATED"
    DIMENSIONS_CONFIGURED = "DIMENSIONS_CONFIGURED"
    EXPERT_CREATED = "EXPERT_CREATED"
    SUPPLIER_CREATED = "SUPPLIER_CREATED"
    CONFLICT_IMPORTED = "CONFLICT_IMPORTED"
    SUPPLIER_BLACKLISTED = "SUPPLIER_BLACKLISTED"


class OutboxEvent(Base):
    """待同步事件。status: PENDING → PROCESSED / FAILED（P1.6 reconciliation 重放）。"""

    __tablename__ = "outbox_event"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    aggregate_id: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(16), server_default="PENDING")
    retry_count: Mapped[int] = mapped_column(Integer, server_default="0")
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime, server_default=text("NOW()"))
    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

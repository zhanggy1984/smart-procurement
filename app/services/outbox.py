"""Outbox 事件写入（P1.3 起步 / P1.6 完善）。

- write_outbox_event：在当前 session 事务内追加事件（与业务写同库同事务，
  业务数据落库则事件必落库，不丢）
- P1.6 补充：write_with_outbox 事务封装 + worker 消费 + reconciliation
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.outbox import OutboxEvent


async def write_outbox_event(
    session: AsyncSession,
    *,
    aggregate_id: str,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    """在当前事务内写入一条 outbox 事件（不 commit，由调用方统一提交）。"""
    session.add(
        OutboxEvent(
            aggregate_id=aggregate_id,
            event_type=event_type,
            payload=payload,
        )
    )

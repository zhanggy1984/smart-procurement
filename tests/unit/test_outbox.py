"""P7.2 OutboxService 单元测试（task.md：4 用例）。

覆盖：事务内写入事件（不 commit，由调用方统一提交）、payload 保留。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.outbox import OutboxEvent
from app.services.outbox import write_outbox_event


@pytest.mark.asyncio
async def test_write_adds_event_without_commit():
    """write_outbox_event 在当前事务内 add，不 commit（同事务不丢事件）。"""
    session = AsyncMock()
    await write_outbox_event(
        session, aggregate_id="EXP-1", event_type="EXPERT_CREATED",
        payload={"name": "张三"},
    )
    session.add.assert_called_once()
    added: OutboxEvent = session.add.call_args[0][0]
    assert added.aggregate_id == "EXP-1"
    assert added.event_type == "EXPERT_CREATED"
    assert added.payload == {"name": "张三"}
    # 关键语义：不 commit（事务原子性由调用方保证）
    session.commit.assert_not_called()


@pytest.mark.asyncio
async def test_write_payload_variants():
    """不同 event_type 与 payload 正常写入。"""
    session = AsyncMock()
    await write_outbox_event(session, aggregate_id="SUP-2", event_type="SUPPLIER_BLACKLISTED",
                             payload={"blacklisted": True})
    added = session.add.call_args[0][0]
    assert added.event_type == "SUPPLIER_BLACKLISTED"
    assert added.payload["blacklisted"] is True

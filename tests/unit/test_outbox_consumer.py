"""P7.2 Outbox 消费（arq worker 链路）单元测试（task.md：消费 + reconciliation）。

覆盖（基建链路，P1.6）：
- consume_pending_once / reconcile_failed：status 分派（PENDING / FAILED 重放）
- _claim_and_process：单事务消费成功 → PROCESSED；单事件失败标记 FAILED 不阻断整批
- _process_event：未知/无需同步类型 → no-op
- _sync_expert / _sync_supplier：MySQL 读当前状态同步；aggregate 缺失 → 不一致告警
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.outbox_consumer import (
    CONSUME_BATCH,
    _claim_and_process,
    _process_event,
    _sync_expert,
    _sync_supplier,
    consume_pending_once,
    reconcile_failed,
)


class _FakeBegin:
    """session.begin() 的假 context manager。

    必须是普通类：AsyncMock 方法 + AsyncMock return_value 时调用返回 coroutine
    （`async with session.begin():` 的 begin() 是同步调用，会崩）。普通类对象
    走真实 async 协议（__aenter__/__aexit__）。
    """

    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


def _sf_ctx(session: AsyncMock):
    """构造 session_factory 的 async context manager mock。

    session_factory() 在 `async with` 里是同步调用（非 await），工厂必须是
    MagicMock；ctx 的 __aenter__/__aexit__ 才是 async 协议。
    """
    sf = MagicMock()
    ctx = AsyncMock()
    ctx.__aenter__.return_value = session
    ctx.__aexit__.return_value = False
    sf.return_value = ctx
    # session 是 AsyncMock：begin 属性本身是 AsyncMock，调用必然返回 coroutine。
    # 用 MagicMock 替换属性（非 AsyncMock），返回普通 context manager 对象。
    session.begin = MagicMock(return_value=_FakeBegin())
    return sf


@pytest.mark.asyncio
async def test_status_dispatch():
    """consume_pending_once 消费 PENDING；reconcile_failed 重放 FAILED。"""
    with patch("app.services.outbox_consumer._claim_and_process", new=AsyncMock(return_value=3)) as cp:
        assert await consume_pending_once() == 3
        cp.assert_awaited_once_with(status="PENDING", limit=CONSUME_BATCH)
        assert await reconcile_failed() == 3
        cp.assert_awaited_with(status="FAILED", limit=CONSUME_BATCH)


@pytest.mark.asyncio
async def test_claim_and_process_success():
    """PENDING 事件消费成功 → PROCESSED，返回条数。"""
    session = AsyncMock()
    rows = [SimpleNamespace(id=1, aggregate_id="EXP-1", event_type="EXPERT_CREATED")]
    row_result = MagicMock()
    row_result.all.return_value = rows
    session.execute.return_value = row_result

    with patch("app.services.outbox_consumer._process_event", new=AsyncMock()) as proc, \
         patch("app.services.outbox_consumer.session_factory", _sf_ctx(session)):
        processed = await _claim_and_process("PENDING", 10)
    assert processed == 1
    proc.assert_awaited_once_with(session, 1, "EXP-1", "EXPERT_CREATED")
    # 状态更新（PROCESSED）至少执行了一次
    assert session.execute.call_count >= 1


@pytest.mark.asyncio
async def test_claim_and_process_event_failure_marks_failed():
    """单事件同步异常 → 标记 FAILED + retry_count+1，不阻断整批（返回 0 成功）。"""
    session = AsyncMock()
    rows = [SimpleNamespace(id=7, aggregate_id="SUP-9", event_type="SUPPLIER_BLACKLISTED")]
    row_result = MagicMock()
    row_result.all.return_value = rows
    session.execute.return_value = row_result

    with patch("app.services.outbox_consumer._process_event",
               new=AsyncMock(side_effect=RuntimeError("neo4j down"))), \
         patch("app.services.outbox_consumer.session_factory", _sf_ctx(session)):
        processed = await _claim_and_process("PENDING", 10)
    assert processed == 0
    # SELECT 拉取 + UPDATE 标记 FAILED
    assert session.execute.call_count == 2


@pytest.mark.asyncio
async def test_process_event_noop():
    """未知/无需同步类型（CONFLICT_IMPORTED 等）→ no-op，不访问聚合。"""
    session = AsyncMock()
    await _process_event(session, 1, "AGG-1", "CONFLICT_IMPORTED")
    session.get.assert_not_called()
    session.execute.assert_not_called()


@pytest.mark.asyncio
async def test_sync_expert_success():
    """按 expert_id 读 MySQL 当前状态同步 Neo4j。"""
    session = AsyncMock()
    expert = MagicMock()
    expert.expert_id = "EXP-1"
    expert.name = "张三"
    expert.organization = "某研究所"
    expert.region = "华中"
    expert.experience = 12
    expert.status = "ACTIVE"
    session.get.return_value = expert
    with patch("app.services.outbox_consumer.neo4j_sync.upsert_expert", new=AsyncMock()) as upsert:
        await _sync_expert(session, "EXP-1")
    upsert.assert_awaited_once_with(
        "EXP-1", name="张三", organization="某研究所",
        region="华中", experience=12, status="ACTIVE",
    )


@pytest.mark.asyncio
async def test_sync_expert_missing_raises():
    """MySQL 无对应专家（不一致）→ 抛错（由 _claim_and_process 标记 FAILED）。"""
    session = AsyncMock()
    session.get.return_value = None
    with pytest.raises(ValueError, match="专家不存在"):
        await _sync_expert(session, "EXP-X")


@pytest.mark.asyncio
async def test_sync_supplier_success():
    """按 supplier_id 读当前状态同步（含 blacklisted）。"""
    session = AsyncMock()
    supplier = MagicMock()
    supplier.supplier_id = "SUP-1"
    supplier.name = "甲供应商"
    supplier.uniform_credit_code = "91310000"
    supplier.legal_person = "李某"
    supplier.industry = "软件"
    supplier.scale = "LARGE"
    supplier.blacklisted = False
    session.get.return_value = supplier
    with patch("app.services.outbox_consumer.neo4j_sync.upsert_supplier", new=AsyncMock()) as upsert:
        await _sync_supplier(session, "SUP-1")
    upsert.assert_awaited_once_with(
        "SUP-1", name="甲供应商", uniform_credit_code="91310000",
        legal_person="李某", industry="软件", scale="LARGE", blacklisted=False,
    )

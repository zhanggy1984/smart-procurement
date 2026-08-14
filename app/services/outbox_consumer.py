"""Outbox 消费与 reconciliation（P1.6）。

- consume_pending_once：SELECT FOR UPDATE SKIP LOCKED 拉取 PENDING → 逐条
  同步 Neo4j → PROCESSED / FAILED（retry_count+1）
- reconcile_failed：每小时扫描 FAILED 重放（MERGE 幂等，重复同步无副作用）

消费策略：payload 仅作事件描述（P1.3/P1.4 写入的 payload 字段不全），同步时
一律从 MySQL 读 aggregate **当前状态**重建——source of truth 是 MySQL。
CONFLICT_IMPORTED 例外：关系明细只写 Neo4j + pending_conflict（无 MySQL 依据），
且 P1.4 commit 后已直同步，worker 视为已处理（no-op）。
"""

from __future__ import annotations

from datetime import datetime, timezone

import structlog
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import session_factory
from app.models.expert import Expert
from app.models.outbox import OutboxEvent, OutboxEventType
from app.models.project import Lot, Project, ScoringDimension
from app.models.supplier import Supplier
from app.services import neo4j_sync

logger = structlog.get_logger(__name__)

# 单批拉取上限（arq cron 每 5s 一批，避免长时间占用锁）
CONSUME_BATCH = 50


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _sync_expert(session: AsyncSession, aggregate_id: str) -> None:
    """按 expert_id 从 MySQL 读当前状态同步。"""
    expert = await session.get(Expert, aggregate_id)
    if expert is None:
        raise ValueError(f"专家不存在: {aggregate_id}（MySQL 与 outbox 不一致）")
    await neo4j_sync.upsert_expert(
        expert.expert_id,
        name=expert.name,
        organization=expert.organization,
        region=expert.region,
        experience=expert.experience,
        status=expert.status,
    )


async def _sync_supplier(session: AsyncSession, aggregate_id: str) -> None:
    """按 supplier_id 读当前状态同步（含 blacklisted 状态）。"""
    supplier = await session.get(Supplier, aggregate_id)
    if supplier is None:
        raise ValueError(f"供应商不存在: {aggregate_id}（MySQL 与 outbox 不一致）")
    await neo4j_sync.upsert_supplier(
        supplier.supplier_id,
        name=supplier.name,
        uniform_credit_code=supplier.uniform_credit_code,
        legal_person=supplier.legal_person,
        industry=supplier.industry,
        scale=supplier.scale,
        blacklisted=supplier.blacklisted,
    )


async def _sync_project(session: AsyncSession, aggregate_id: str) -> None:
    project = await session.get(Project, aggregate_id)
    if project is None:
        raise ValueError(f"项目不存在: {aggregate_id}（MySQL 与 outbox 不一致）")
    await neo4j_sync.upsert_project(
        project.project_id,
        project_code=project.project_code,
        name=project.name,
        type=project.type,
        region=project.region,
        budget=project.budget,
        status=project.status,
    )


async def _sync_lot(session: AsyncSession, aggregate_id: str) -> None:
    lot = await session.get(Lot, aggregate_id)
    if lot is None:
        raise ValueError(f"标段不存在: {aggregate_id}（MySQL 与 outbox 不一致）")
    await neo4j_sync.upsert_lot(
        lot.lot_id,
        lot.project_id,
        lot_code=lot.lot_code,
        name=lot.name,
        budget=lot.budget,
        status=lot.status,
    )


async def _sync_dimensions(session: AsyncSession, aggregate_id: str) -> None:
    """aggregate_id 为 lot_id，同步该标段全部维度节点 + 关系。"""
    dims = (
        await session.scalars(select(ScoringDimension).where(ScoringDimension.lot_id == aggregate_id))
    ).all()
    for dim in dims:
        await neo4j_sync.upsert_dimension(
            dim.dimension_id,
            dim.lot_id,
            name=dim.name,
            max_score=dim.max_score,
            weight=dim.weight,
        )


# event_type → 同步函数。CONFLICT_IMPORTED 无 MySQL 依据（no-op）。
_HANDLERS: dict[str, object] = {
    OutboxEventType.EXPERT_CREATED: _sync_expert,
    OutboxEventType.SUPPLIER_CREATED: _sync_supplier,
    OutboxEventType.SUPPLIER_BLACKLISTED: _sync_supplier,
    OutboxEventType.PROJECT_CREATED: _sync_project,
    OutboxEventType.LOT_CREATED: _sync_lot,
    OutboxEventType.DIMENSIONS_CONFIGURED: _sync_dimensions,
}


async def _process_event(session: AsyncSession, event_id: int, aggregate_id: str, event_type: str) -> None:
    """同步单个事件。未知/无需同步类型视为已处理；同步异常向上抛（标记 FAILED）。"""
    handler = _HANDLERS.get(event_type)
    if handler is None:
        logger.info("outbox.event_noop", event_id=event_id, event_type=event_type)
        return
    await handler(session, aggregate_id)


async def _claim_and_process(status: str, limit: int) -> int:
    """拉取一批 status 事件（FOR UPDATE SKIP LOCKED）→ 处理 → 更新状态，单事务。

    SKIP LOCKED 保证多 worker 并发不重复消费；事务结束（commit）释放行锁。
    返回处理条数。
    """
    processed = 0
    async with session_factory() as session:
        async with session.begin():
            rows = (
                await session.execute(
                    text(
                        "SELECT id, aggregate_id, event_type FROM outbox_event "
                        "WHERE status=:status ORDER BY id LIMIT :limit FOR UPDATE SKIP LOCKED"
                    ),
                    {"status": status, "limit": limit},
                )
            ).all()
            for r in rows:
                event_id, aggregate_id, event_type = r.id, r.aggregate_id, r.event_type
                try:
                    await _process_event(session, event_id, aggregate_id, event_type)
                except Exception as e:  # noqa: BLE001  单事件失败不影响整批
                    logger.warning("outbox.event_failed", event_id=event_id, event_type=event_type, error=str(e))
                    await session.execute(
                        update(OutboxEvent)
                        .where(OutboxEvent.id == event_id)
                        .values(status="FAILED", retry_count=OutboxEvent.retry_count + 1)
                    )
                    continue
                await session.execute(
                    update(OutboxEvent)
                    .where(OutboxEvent.id == event_id)
                    .values(status="PROCESSED", processed_at=_now())
                )
                processed += 1
    return processed


async def consume_pending_once(limit: int = CONSUME_BATCH) -> int:
    """消费一批 PENDING 事件（arq worker 每 5s 调用）。返回处理成功条数。"""
    return await _claim_and_process(status="PENDING", limit=limit)


async def reconcile_failed(limit: int = CONSUME_BATCH) -> int:
    """Reconciliation：扫描 FAILED 事件重放（每小时 + 验收手动触发）。"""
    return await _claim_and_process(status="FAILED", limit=limit)

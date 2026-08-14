"""企查查冲突导入服务（P1.4）。

CSV 行 → 匹配专家（按姓名）+ 供应商（信用代码优先，其次企业名）：
- 双匹配且关系类型合法 → Neo4j 回避关系（QCC_RELATION_TO_NEO4J 映射）
- 人匹配企业未匹配 → pending_conflict 冷数据（PENDING，供应商入库时唤醒）
- 企业匹配人未匹配 / 关系类型未知 → 跳过计数

单事务写 pending_conflict + outbox CONFLICT_IMPORTED，Neo4j 关系在 commit 后直同步
（失败仅告警）。值非法（未知关系类型/未知企业）不整批失败，走计数——企查查
真实数据噪声大，一行坏数据不应阻断整批。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import structlog
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import QCC_RELATION_TO_NEO4J
from app.core.crypto import generate_id
from app.models.expert import Expert
from app.models.outbox import OutboxEventType
from app.models.pending_conflict import PendingConflict, PendingConflictStatus
from app.models.supplier import Supplier
from app.services import neo4j_sync
from app.services.outbox import write_outbox_event

logger = structlog.get_logger(__name__)


async def _sync_neo4j(name: str, coro) -> None:
    """执行 Neo4j 同步，失败仅告警（outbox 事件可兜底重放）。"""
    try:
        await coro
    except Exception as e:  # noqa: BLE001
        logger.warning("neo4j_sync_failed", operation=name, error=str(e))


def _parse_ratio(raw: str) -> Optional[float]:
    """持股比例解析：支持小数（0.05）与百分比（5%）→ 返回小数比例。"""
    value = (raw or "").strip()
    if not value:
        return None
    try:
        if value.endswith("%"):
            return round(float(value[:-1]) / 100, 4)
        return round(float(value), 4)
    except ValueError:
        return None


async def list_pending_conflicts(
    session: AsyncSession,
    *,
    status: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict:
    """工商信息冷数据列表（分页 + 状态筛选，P6.2 补：工商信息页数据源）。"""
    stmt = select(PendingConflict).order_by(PendingConflict.created_at.desc())
    count_stmt = select(func.count()).select_from(PendingConflict)
    if status:
        stmt = stmt.where(PendingConflict.status == status)
        count_stmt = count_stmt.where(PendingConflict.status == status)
    total = (await session.scalar(count_stmt)) or 0
    rows = (await session.scalars(stmt.offset((page - 1) * page_size).limit(page_size))).all()
    return {
        "total": total,
        "items": [
            {
                "id": r.id,
                "person_name": r.person_name,
                "company_name": r.company_name,
                "credit_code": r.credit_code,
                "relation_type": r.relation_type,
                "expert_id": r.expert_id,
                "status": r.status,
                "created_at": r.created_at,
            }
            for r in rows
        ],
    }


async def import_conflicts(
    session: AsyncSession,
    rows: list[dict],
    *,
    operator_id: str,
) -> dict:
    """批量导入企查查冲突关系（单事务写冷数据 + outbox）。

    返回计数: total / matched / pending / person_unmatched / unknown_relation。
    """
    # ---- 预载匹配表（IN 查询，避免全表扫描） ----
    person_names = {row["姓名"] for row in rows if row.get("姓名")}
    credit_codes = {row["统一社会信用代码"] for row in rows if row.get("统一社会信用代码")}
    company_names = {row["企业名称"] for row in rows if row.get("企业名称")}

    expert_by_name: dict[str, str] = {}
    if person_names:
        for e in await session.scalars(select(Expert).where(Expert.name.in_(person_names))):
            expert_by_name.setdefault(e.name, e.expert_id)  # 重名取第一个（记一次 warning）

    supplier_by_credit: dict[str, str] = {}
    supplier_by_name: dict[str, str] = {}
    if credit_codes or company_names:
        conditions = []
        if credit_codes:
            conditions.append(Supplier.uniform_credit_code.in_(credit_codes))
        if company_names:
            conditions.append(Supplier.name.in_(company_names))
        for s in await session.scalars(select(Supplier).where(or_(*conditions))):
            if s.uniform_credit_code:
                supplier_by_credit.setdefault(s.uniform_credit_code, s.supplier_id)
            supplier_by_name.setdefault(s.name, s.supplier_id)

    matched = pending = person_unmatched = unknown_relation = 0
    pending_rows: list[PendingConflict] = []
    neo4j_rels: list[tuple] = []  # (relation_type, expert_id, supplier_id, props)
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    for idx, row in enumerate(rows, start=2):  # 第 1 行为表头
        person_name = (row.get("姓名") or "").strip()
        company_name = (row.get("企业名称") or "").strip()
        credit_code = (row.get("统一社会信用代码") or "").strip()
        relation_type = (row.get("关系类型") or "").strip()
        role = (row.get("职位") or "").strip() or None
        ratio_raw = (row.get("持股比例") or "").strip()

        expert_id = expert_by_name.get(person_name)
        supplier_id = supplier_by_credit.get(credit_code) or supplier_by_name.get(company_name)

        # 人未匹配（无论企业是否匹配）→ 跳过
        if expert_id is None:
            person_unmatched += 1
            continue

        # 人匹配企业未匹配 → 冷数据（供应商入库时唤醒）
        if supplier_id is None:
            pending_rows.append(
                PendingConflict(
                    person_name=person_name,
                    company_name=company_name or None,
                    credit_code=credit_code or None,
                    relation_type=relation_type or None,
                    expert_id=expert_id,
                    supplier_id=None,
                    status=PendingConflictStatus.PENDING,
                    created_at=now,
                )
            )
            pending += 1
            continue

        # 双匹配：关系类型映射
        rel_type = QCC_RELATION_TO_NEO4J.get(relation_type)
        if rel_type is None:
            unknown_relation += 1
            logger.warning("conflict_relation_unknown", line=idx, relation_type=relation_type)
            continue

        props: dict = {}
        if rel_type == "EMPLOYED_BY":
            # 企查查当前任职快照：endDate 缺失表达"当前"（Neo4j null 属性不允许）
            props = {"role": role, "startDate": None, "endDate": None}
        elif rel_type == "HOLDS_SHARE":
            ratio = _parse_ratio(ratio_raw)
            if ratio is not None:
                props = {"ratio": ratio}
        neo4j_rels.append((rel_type, expert_id, supplier_id, props))
        matched += 1

    batch_id = generate_id("CFL")
    session.add_all(pending_rows)
    await write_outbox_event(
        session,
        aggregate_id=batch_id,
        event_type=OutboxEventType.CONFLICT_IMPORTED,
        payload={
            "batch_id": batch_id,
            "matched": matched,
            "pending": pending,
            "person_unmatched": person_unmatched,
            "unknown_relation": unknown_relation,
        },
    )
    await session.commit()

    # ---- commit 后直同步 Neo4j 关系 ----
    for rel_type, expert_id, supplier_id, props in neo4j_rels:
        await _sync_neo4j(
            "upsert_conflict_relation",
            neo4j_sync.upsert_conflict_relation(
                rel_type,
                expert_id=expert_id,
                supplier_id=supplier_id,
                **props,
            ),
        )
    logger.info(
        "conflicts_imported",
        total=len(rows),
        matched=matched,
        pending=pending,
        person_unmatched=person_unmatched,
        unknown_relation=unknown_relation,
        operator=operator_id,
    )
    return {
        "total": len(rows),
        "matched": matched,
        "pending": pending,
        "person_unmatched": person_unmatched,
        "unknown_relation": unknown_relation,
    }

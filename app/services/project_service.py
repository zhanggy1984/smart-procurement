"""项目管理服务（P1.3）。

业务规则（solution.md / task.md P1.3）：
- SUM(lot.budget) ≤ project.budget（创建标段时校验）
- SUM(dimension.weight) = 1.0 ± 0.001（配置维度时校验）
- expert_count ≥ min_experts_per_dimension（配置遴选时校验）

Neo4j 同步策略（P1.3 过渡）：
1. MySQL 事务内写业务表 + outbox_event（同库同事务，不丢）
2. 事务提交后**直接**同步 Neo4j（MERGE 幂等）→ 满足验收即时可见
3. P1.6 起由 worker 消费 outbox 驱动同步（本函数保留，worker 复用），
   直接同步失败仅告警——outbox 事件已在，Reconciliation 可兜底。
"""

from __future__ import annotations

from decimal import Decimal

import structlog
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import generate_id
from app.models.outbox import OutboxEventType
from app.models.project import (
    Lot,
    LotExpertCriteria,
    Project,
    ScoringCriterion,
    ScoringDimension,
)
from app.schemas.project import (
    DimensionCreate,
    ExpertCriteriaCreate,
    LotCreate,
    ProjectCreate,
)
from app.services import neo4j_sync
from app.services.outbox import write_outbox_event

logger = structlog.get_logger(__name__)

# 权重和校验用 Decimal（weight 为 Numeric 存 Decimal，不能与 float 混算）
_WEIGHT_ONE = Decimal("1.0")
_WEIGHT_TOLERANCE = Decimal("0.001")


class ProjectNotFoundError(ValueError):
    """项目不存在。"""


class LotNotFoundError(ValueError):
    """标段不存在。"""


class BudgetExceededError(ValueError):
    """标段预算总和超项目预算。"""


class WeightSumError(ValueError):
    """维度权重和不为 1.0。"""


class ExpertCriteriaError(ValueError):
    """专家遴选参数非法。"""


class ProjectCodeTakenError(ValueError):
    """项目编码已存在。"""


async def _sync_neo4j(name: str, coro) -> None:
    """执行 Neo4j 同步，失败仅告警（outbox 事件可兜底重放）。"""
    try:
        await coro
    except Exception as e:  # noqa: BLE001  Neo4j 短暂不可用时不应阻断 MySQL 主链路
        logger.warning("neo4j_sync_failed", operation=name, error=str(e))


async def create_project(
    session: AsyncSession,
    data: ProjectCreate,
    *,
    operator_id: str,
) -> Project:
    """创建项目：project_code 唯一 + 写 MySQL + outbox + Neo4j。"""
    existing = await session.scalar(
        select(Project).where(Project.project_code == data.project_code)
    )
    if existing is not None:
        raise ProjectCodeTakenError(f"项目编码已存在: {data.project_code}")

    project = Project(
        project_id=generate_id("PRJ"),
        project_code=data.project_code,
        name=data.name,
        type=data.type,
        region=data.region,
        budget=data.budget,
        status="DRAFT",
        managed_by=data.managed_by,
    )
    session.add(project)
    await write_outbox_event(
        session,
        aggregate_id=project.project_id,
        event_type=OutboxEventType.PROJECT_CREATED,
        payload={"project_id": project.project_id, "project_code": project.project_code},
    )
    await session.commit()
    await session.refresh(project)

    await _sync_neo4j(
        "upsert_project",
        neo4j_sync.upsert_project(
            project.project_id,
            project_code=project.project_code,
            name=project.name,
            type=project.type,
            region=project.region,
            budget=project.budget,
            status=project.status,
        ),
    )
    logger.info("project_created", project_id=project.project_id, operator=operator_id)
    return project


async def list_projects(
    session: AsyncSession,
    *,
    page: int = 1,
    page_size: int = 20,
    keyword: str | None = None,
) -> tuple[list[Project], int]:
    """分页查询项目（P6.3 项目列表页）。keyword 模糊匹配 name/code。"""
    from sqlalchemy import func, select

    stmt = select(Project)
    if keyword:
        like = f"%{keyword}%"
        stmt = stmt.where((Project.name.like(like)) | (Project.project_code.like(like)))
    total = (
        await session.execute(select(func.count()).select_from(stmt.subquery()))
    ).scalar_one()
    rows = (
        await session.execute(
            stmt.order_by(Project.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()
    return list(rows), total


async def list_lots(
    session: AsyncSession,
    *,
    page: int = 1,
    page_size: int = 20,
    status: str | None = None,
) -> tuple[list[dict], int]:
    """标段列表（P6.3 围串标待办）：关联项目名 + 标书数，可按状态过滤。"""
    from app.models.bid_document import BidDocument

    base = select(Lot).join(Project, Lot.project_id == Project.project_id)
    if status:
        base = base.where(Lot.status == status)
    total = (await session.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = (
        await session.execute(
            base.order_by(Lot.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()

    lot_ids = [l.lot_id for l in rows]
    bid_counts: dict[str, int] = {}
    if lot_ids:
        bid_counts = dict(
            (
                await session.execute(
                    select(BidDocument.lot_id, func.count())
                    .where(BidDocument.lot_id.in_(lot_ids))
                    .group_by(BidDocument.lot_id)
                )
            ).all()
        )
    project_ids = {l.project_id for l in rows}
    proj_rows = (
        await session.execute(
            select(Project.project_id, Project.project_code, Project.name).where(
                Project.project_id.in_(project_ids)
            )
        )
    ).all()
    proj_map = {p.project_id: (p.project_code, p.name) for p in proj_rows}

    items = []
    for l in rows:
        code, pname = proj_map.get(l.project_id, ("", ""))
        items.append(
            {
                "lot_id": l.lot_id,
                "lot_code": l.lot_code,
                "name": l.name,
                "budget": l.budget,
                "status": l.status,
                "project_id": l.project_id,
                "project_code": code,
                "project_name": pname,
                "bid_count": bid_counts.get(l.lot_id, 0),
            }
        )
    return items, total


async def get_project(session: AsyncSession, project_id: str) -> Project | None:
    """项目详情（含标段列表，懒加载触发）。"""
    return await session.get(Project, project_id)


async def create_lot(session: AsyncSession, project_id: str, data: LotCreate) -> Lot:
    """创建标段：校验 SUM(lot.budget)+new ≤ project.budget。"""
    project = await session.get(Project, project_id)
    if project is None:
        raise ProjectNotFoundError(f"项目不存在: {project_id}")

    current_budget = await session.scalar(
        select(func.coalesce(func.sum(Lot.budget), 0)).where(Lot.project_id == project_id)
    )
    if current_budget + data.budget > project.budget:
        raise BudgetExceededError(
            f"标段预算总和 {current_budget + data.budget} 超项目预算 {project.budget}"
        )

    lot = Lot(
        lot_id=generate_id("LOT"),
        project_id=project_id,
        lot_code=data.lot_code,
        name=data.name,
        budget=data.budget,
        status="BIDDING",
    )
    session.add(lot)
    await write_outbox_event(
        session,
        aggregate_id=lot.lot_id,
        event_type=OutboxEventType.LOT_CREATED,
        payload={"lot_id": lot.lot_id, "project_id": project_id, "lot_code": lot.lot_code},
    )
    await session.commit()
    await session.refresh(lot)

    await _sync_neo4j(
        "upsert_lot",
        neo4j_sync.upsert_lot(
            lot.lot_id,
            project_id,
            lot_code=lot.lot_code,
            name=lot.name,
            budget=lot.budget,
            status=lot.status,
        ),
    )
    logger.info("lot_created", lot_id=lot.lot_id, project_id=project_id)
    return lot


async def add_dimensions(
    session: AsyncSession,
    lot_id: str,
    dimensions: list[DimensionCreate],
) -> list[ScoringDimension]:
    """配置标段评分维度（覆盖式）：校验权重和 + 删除重建 + outbox + Neo4j。"""
    lot = await session.get(Lot, lot_id)
    if lot is None:
        raise LotNotFoundError(f"标段不存在: {lot_id}")

    weight_sum = sum((d.weight for d in dimensions), Decimal("0"))
    if abs(weight_sum - _WEIGHT_ONE) > _WEIGHT_TOLERANCE:
        raise WeightSumError(f"维度权重和 {weight_sum} 必须为 1.0 ± {_WEIGHT_TOLERANCE}")

    # 覆盖式：删除该标段旧维度及其子项，再插入新配置
    old_dimensions = await session.scalars(
        select(ScoringDimension).where(ScoringDimension.lot_id == lot_id)
    )
    old_ids = [d.dimension_id for d in old_dimensions]
    if old_ids:
        await session.execute(
            delete(ScoringCriterion).where(ScoringCriterion.dimension_id.in_(old_ids))
        )
        await session.execute(
            delete(ScoringDimension).where(ScoringDimension.lot_id == lot_id)
        )

    created: list[ScoringDimension] = []
    payload_dimensions: list[dict] = []
    for idx, d in enumerate(dimensions, start=1):
        dim = ScoringDimension(
            dimension_id=f"DIM-{lot_id}-{idx}",
            lot_id=lot_id,
            name=d.name,
            max_score=d.max_score,
            weight=d.weight,
            sort_order=d.sort_order,
        )
        session.add(dim)
        for cidx, c in enumerate(d.criteria, start=1):
            session.add(
                ScoringCriterion(
                    criterion_id=f"CRI-{dim.dimension_id}-{cidx}",
                    dimension_id=dim.dimension_id,
                    name=c.name,
                    description=c.description,
                    scoring_rubric=c.scoring_rubric,
                    max_score=c.max_score,
                    sort_order=cidx,
                )
            )
        created.append(dim)
        payload_dimensions.append(
            {"dimension_id": dim.dimension_id, "name": d.name, "max_score": str(d.max_score)}
        )

    await write_outbox_event(
        session,
        aggregate_id=lot_id,
        event_type=OutboxEventType.DIMENSIONS_CONFIGURED,
        payload={"lot_id": lot_id, "dimensions": payload_dimensions},
    )
    await session.commit()

    for dim in created:
        await _sync_neo4j(
            "upsert_dimension",
            neo4j_sync.upsert_dimension(
                dim.dimension_id,
                lot_id,
                name=dim.name,
                max_score=dim.max_score,
                weight=dim.weight,
            ),
        )
    logger.info("dimensions_configured", lot_id=lot_id, count=len(created))
    return created


async def configure_expert_criteria(
    session: AsyncSession,
    lot_id: str,
    data: ExpertCriteriaCreate,
) -> LotExpertCriteria:
    """配置专家遴选参数：权重和 = 1.0 + expert_count ≥ min。"""
    lot = await session.get(Lot, lot_id)
    if lot is None:
        raise LotNotFoundError(f"标段不存在: {lot_id}")

    weights = [
        data.weight_specialization,
        data.weight_experience,
        data.weight_review_quality,
        data.weight_region,
    ]
    weight_sum = sum(weights, Decimal("0"))
    if abs(weight_sum - _WEIGHT_ONE) > _WEIGHT_TOLERANCE:
        raise WeightSumError(f"遴选权重和 {weight_sum} 必须为 1.0 ± {_WEIGHT_TOLERANCE}")
    if data.expert_count < data.min_experts_per_dimension:
        raise ExpertCriteriaError(
            f"expert_count({data.expert_count}) 必须 ≥ min_experts_per_dimension({data.min_experts_per_dimension})"
        )

    criteria = await session.get(LotExpertCriteria, lot_id)
    if criteria is None:
        criteria = LotExpertCriteria(lot_id=lot_id)
        session.add(criteria)
    criteria.expert_count = data.expert_count
    criteria.min_experts_per_dimension = data.min_experts_per_dimension
    criteria.weight_specialization = data.weight_specialization
    criteria.weight_experience = data.weight_experience
    criteria.weight_review_quality = data.weight_review_quality
    criteria.weight_region = data.weight_region
    criteria.min_experience = data.min_experience
    await session.commit()
    await session.refresh(criteria)

    logger.info("expert_criteria_configured", lot_id=lot_id)
    return criteria

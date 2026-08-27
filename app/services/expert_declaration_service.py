"""专家回避申报（P4.3）。

- list_my_assignments()：我的任务列表（assignment + 标段 + 状态）
- get_declaration()：待申报供应商（标段投标商，标注系统已检测的冲突）
- declare()：提交申报
  - 全部确认无冲突 → assignment IN_PROGRESS（可进入评审）+ 通知
  - 申报冲突 → 写 expert_conflict_declaration + Neo4j 关系同步 →
    assignment CONFLICT_DECLARED → 自动补匹配（新专家收通知）

验收两路径：无冲突→评审；冲突→补匹配→新专家收到通知。
"""

from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bid_document import BidDocument, BidStatus
from app.models.expert import Expert
from app.models.expert_conflict_declaration import ExpertConflictDeclaration
from app.models.expert_review import ExpertReview, ReviewStatus
from app.models.lot_expert_assignment import AssignmentStatus, LotExpertAssignment
from app.models.project import Lot, Project, ScoringDimension
from app.models.supplier import Supplier
from app.services import notification_service as notification
from app.services.expert_match_service import _find_conflicts

logger = structlog.get_logger(__name__)


class AssignmentNotFoundError(ValueError):
    """分配不存在 → 404。"""


class AssignmentAccessDeniedError(ValueError):
    """非本人分配 → 403。"""


class AlreadyDeclaredError(ValueError):
    """已申报过（状态非 PENDING_DECLARATION）→ 409。"""


class NoConflictSupplierError(ValueError):
    """确认列表缺少该标段全部投标商 → 400。"""


async def list_my_assignments(session: AsyncSession, expert_id: str) -> list[dict]:
    """我的任务列表（待申报/待评审/已处理，前端分 tab）。

    每个 assignment 附带：
    - dimensions：我负责的维度（名称/满分）
    - bids：该标段 FROZEN 标书 × 我负责维度的评审矩阵（review_id/status/score，
      未创建评审的格子 review_status=None）——评审工作台数据底座。
    """
    rows = (
        await session.scalars(
            select(LotExpertAssignment).where(LotExpertAssignment.expert_id == expert_id)
        )
    ).all()
    out = []
    for a in rows:
        lot = await session.get(Lot, a.lot_id)
        project = await session.get(Project, lot.project_id) if lot else None
        dim_ids = a.dimension_ids or []
        dims = (
            await session.scalars(
                select(ScoringDimension)
                .where(ScoringDimension.lot_id == a.lot_id, ScoringDimension.dimension_id.in_(dim_ids))
                .order_by(ScoringDimension.sort_order)
            )
        ).all() if dim_ids else []

        # 标书 + 供应商名
        bid_rows = (
            await session.execute(
                select(BidDocument, Supplier)
                .join(Supplier, BidDocument.supplier_id == Supplier.supplier_id)
                .where(BidDocument.lot_id == a.lot_id, BidDocument.status == BidStatus.FROZEN)
            )
        ).all()
        bid_ids = [b.bid_id for b, _ in bid_rows]
        reviews = (
            await session.scalars(
                select(ExpertReview).where(
                    ExpertReview.bid_id.in_(bid_ids),
                    ExpertReview.expert_id == expert_id,
                )
            )
        ).all() if bid_ids else []
        review_map = {(r.bid_id, r.dimension_id): r for r in reviews}

        bids = []
        for bid, supplier in bid_rows:
            bids.append({
                "bid_id": bid.bid_id,
                "supplier_name": supplier.name,
                "status": bid.status,
                "dimensions": [
                    {
                        "dimension_id": d.dimension_id,
                        "dimension_name": d.name,
                        "max_score": float(d.max_score or 0),
                        "review_id": review_map[(bid.bid_id, d.dimension_id)].review_id
                        if (bid.bid_id, d.dimension_id) in review_map else None,
                        "review_status": review_map[(bid.bid_id, d.dimension_id)].status
                        if (bid.bid_id, d.dimension_id) in review_map else None,
                        "score": float(review_map[(bid.bid_id, d.dimension_id)].score)
                        if (bid.bid_id, d.dimension_id) in review_map
                        and review_map[(bid.bid_id, d.dimension_id)].score is not None else None,
                    }
                    for d in dims
                ],
            })

        out.append(
            {
                "assignment_id": a.id,
                "lot_id": a.lot_id,
                "lot_name": lot.name if lot else "",
                "project_name": project.name if project else "",
                "dimensions": [
                    {"dimension_id": d.dimension_id, "name": d.name,
                     "max_score": float(d.max_score or 0)}
                    for d in dims
                ],
                "status": a.status,
                "assigned_at": a.assigned_at.isoformat() if a.assigned_at else None,
                "bids": bids,
            }
        )
    return out


async def get_declaration(
    session: AsyncSession, *, assignment_id: int, expert_id: str
) -> dict:
    """待申报供应商列表（标段投标商 + 系统已检测冲突标注）。"""
    assignment = await session.get(LotExpertAssignment, assignment_id)
    if assignment is None:
        raise AssignmentNotFoundError(f"分配不存在: {assignment_id}")
    if assignment.expert_id != expert_id:
        raise AssignmentAccessDeniedError("不能查看非本人分配")

    from app.models.bid_document import BidDocument

    suppliers = list(
        (
            await session.scalars(
                select(BidDocument.supplier_id)
                .where(BidDocument.lot_id == assignment.lot_id)
                .distinct()
            )
        ).all()
    )
    conflicts = await _find_conflicts([expert_id], suppliers)
    rels = conflicts.get(expert_id, [])
    return {
        "assignment_id": assignment_id,
        "lot_id": assignment.lot_id,
        "suppliers": [
            {
                "supplier_id": s,
                # 系统已检测到的冲突类型（Neo4j），专家可确认或补充申报
                "known_conflicts": [r for r in rels if r in
                                    ("EMPLOYED_BY", "HOLDS_SHARE", "RELATIVE_EMPLOYED")],
            }
            for s in suppliers
        ],
        "status": assignment.status,
    }


async def _supplement(
    session: AsyncSession, assignment: LotExpertAssignment
) -> str | None:
    """自动补匹配：该标段候选池补 1 位无冲突、未分配的 ACTIVE 专家。

    候选取项目地区内专家（标签严格匹配需 P4.2 的 tags 入参，此处放宽到地区），
    返回补入的 expert_id；无可补返回 None。
    """
    lot = await session.get(Lot, assignment.lot_id)
    project = await session.get(Project, lot.project_id) if lot else None
    region = project.region if project else None
    assigned = list(
        (
            await session.scalars(
                select(LotExpertAssignment.expert_id).where(
                    LotExpertAssignment.lot_id == assignment.lot_id
                )
            )
        ).all()
    )
    candidates = (
        await session.scalars(
            select(Expert.expert_id)
            .where(Expert.status == "ACTIVE",
                   Expert.region == region if region else True,
                   Expert.expert_id.not_in(assigned + [assignment.expert_id]))
        )
    ).all()

    from app.models.bid_document import BidDocument

    suppliers = list(
        (
            await session.scalars(
                select(BidDocument.supplier_id)
                .where(BidDocument.lot_id == assignment.lot_id)
                .distinct()
            )
        ).all()
    )
    conflicts = await _find_conflicts([c for c in candidates], suppliers)
    for expert_id in candidates:
        if expert_id not in conflicts:
            session.add(
                LotExpertAssignment(
                    lot_id=assignment.lot_id,
                    expert_id=expert_id,
                    dimension_ids=assignment.dimension_ids or [],
                    status=AssignmentStatus.PENDING_DECLARATION,
                )
            )
            await session.commit()
            logger.info("declare.supplement", lot_id=assignment.lot_id, new_expert=expert_id)
            return expert_id
    return None


async def declare(
    session: AsyncSession,
    *,
    assignment_id: int,
    expert_id: str,
    confirmations: list[dict],
) -> dict:
    """提交回避申报（逐供应商确认/申报）。返回新状态与补匹配结果。"""
    assignment = await session.get(LotExpertAssignment, assignment_id)
    if assignment is None:
        raise AssignmentNotFoundError(f"分配不存在: {assignment_id}")
    if assignment.expert_id != expert_id:
        raise AssignmentAccessDeniedError("不能申报非本人分配")
    if assignment.status != AssignmentStatus.PENDING_DECLARATION:
        raise AlreadyDeclaredError(f"当前状态 {assignment.status} 已处理，不可重复申报")

    conflict_items = [c for c in confirmations if c.get("has_conflict")]
    lot_id = assignment.lot_id

    if conflict_items:
        # 写入申报记录 + Neo4j 关系同步（MERGE 幂等）
        from app.services import neo4j_sync

        for c in conflict_items:
            rel = c.get("relation_type") or "EMPLOYED_BY"
            detail = c.get("relation_detail")
            session.add(
                ExpertConflictDeclaration(
                    assignment_id=assignment.id,
                    expert_id=expert_id,
                    lot_id=lot_id,
                    supplier_id=c.get("supplier_id"),
                    relation_type=rel,
                    relation_detail=detail,
                )
            )
            # P8 异常兜底：Neo4j 同步失败仅告警（图暂不可用），不回滚 MySQL 申报事务——
            # 申报记录是权威事实，图关系同步由 worker reconcile 补（MERGE 幂等）
            try:
                await neo4j_sync.upsert_conflict_relation(
                    rel, expert_id=expert_id, supplier_id=c.get("supplier_id")
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("declare.graph_sync_failed", assignment_id=assignment_id,
                               expert_id=expert_id, relation=rel, error=str(e))
        assignment.status = AssignmentStatus.CONFLICT_DECLARED
        await session.commit()

        new_expert = await _supplement(session, assignment)
        await notification.send_to_expert(
            session, expert_id=expert_id, type="DECLARATION_RESULT",
            title="回避申报已提交",
            content=f"已申报 {len(conflict_items)} 项回避关系，该标段将自动补充匹配专家。",
            related_id=str(lot_id),
        )
        if new_expert:
            await notification.send_to_expert(
                session, expert_id=new_expert, type="ASSIGNMENT_NOTICE",
                title="新的评审任务分配",
                content=f"您被补充匹配到标段 {lot_id}，请尽快完成回避申报。",
                related_id=str(lot_id),
            )
        logger.info("declare.conflict", assignment_id=assignment_id, conflicts=len(conflict_items),
                    supplemented=new_expert)
        return {
            "assignment_id": assignment_id,
            "status": AssignmentStatus.CONFLICT_DECLARED,
            "declared_conflicts": [c.get("supplier_id") for c in conflict_items],
            "supplemented_expert": new_expert,
        }

    # 全部确认无冲突 → IN_PROGRESS（可进入评审）
    assignment.status = AssignmentStatus.IN_PROGRESS
    await session.commit()
    await notification.send_to_expert(
        session, expert_id=expert_id, type="DECLARATION_RESULT",
        title="回避申报完成，可进入评审",
        content=f"标段 {lot_id} 回避申报已全部确认，无冲突，现在可以进行评审。",
        related_id=str(lot_id),
    )
    logger.info("declare.clean", assignment_id=assignment_id)
    return {"assignment_id": assignment_id, "status": AssignmentStatus.IN_PROGRESS,
            "declared_conflicts": [], "supplemented_expert": None}

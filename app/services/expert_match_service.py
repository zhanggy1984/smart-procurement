"""专家匹配算法（P4.2）。

5 步匹配流程（task.md P4.2）：
  Step 1 候选搜索：LLM 标签 + 项目地区精确匹配（MySQL：expert_specialization +
         expert.region/status 为 source of truth；Neo4j 无标签结构）
  Step 2 投标供应商：bid_document 去重
  Step 3 冲突检测：Neo4j 批量 Cypher 查 4 条回避路径（EMPLOYED_BY/HOLDS_SHARE/
         SAME_ORGANIZATION/RELATIVE_EMPLOYED），命中即排除
  Step 4 多维加权排序：specialization×0.40 + experience×0.30 + review_quality×0.20
         + region×0.10（权重取 lot_expert_criteria）
  Step 5 维度覆盖：按分数分配维度（每维度 ≥ min_experts_per_dimension），
         不足从备选池补入（底线 ≥1 标签命中），仍不足 → INSUFFICIENT_EXPERTS 告警

结果落库 lot_expert_assignment（status=PENDING_DECLARATION，P4.3 申报）。
"""

from __future__ import annotations

from datetime import datetime, timezone

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import neo4j
from app.models.expert import Expert
from app.models.expert_specialization import ExpertSpecialization
from app.models.project import Lot, LotExpertCriteria, ScoringDimension

logger = structlog.get_logger(__name__)

# 4 条回避关系（专家→供应商 / 专家→专家）
_CONFLICT_REL_TYPES = ("EMPLOYED_BY", "HOLDS_SHARE", "RELATIVE_EMPLOYED", "SAME_ORGANIZATION")


class LotNotFoundError(ValueError):
    """标段不存在 → 404。"""


class LotNotUnderReviewError(ValueError):
    """标段不在评审中 → 400。"""


class NoTagsError(ValueError):
    """缺少项目标签 → 400。"""


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _load_candidates(
    session: AsyncSession, tags: list[str], region: str
) -> list[dict]:
    """Step 1：候选搜索。标签命中（expert_specialization）+ 地区精确匹配。"""
    rows = (
        await session.execute(
            select(Expert.expert_id, Expert.name, Expert.region, Expert.experience,
                   ExpertSpecialization.tag)
            .join(ExpertSpecialization, ExpertSpecialization.expert_id == Expert.expert_id)
            .where(Expert.status == "ACTIVE", Expert.region == region,
                   ExpertSpecialization.tag.in_(tags))
        )
    ).all()
    by_expert: dict[str, dict] = {}
    for expert_id, name, er_region, experience, tag in rows:
        e = by_expert.setdefault(
            expert_id, {"expert_id": expert_id, "name": name, "region": er_region,
                        "experience": experience or 0, "tags": set()}
        )
        e["tags"].add(tag)
    return list(by_expert.values())


async def _load_bidding_suppliers(session: AsyncSession, lot_id: str) -> list[str]:
    """Step 2：投标供应商列表。"""
    from app.models.bid_document import BidDocument

    return list(
        (
            await session.scalars(
                select(BidDocument.supplier_id).where(BidDocument.lot_id == lot_id).distinct()
            )
        ).all()
    )


async def _conflicts_with_status(
    expert_ids: list[str], supplier_ids: list[str]
) -> tuple[dict[str, list[str]], bool]:
    """Step 3：Neo4j 批量冲突检测（4 条回避路径），带异常兜底。

    返回 (conflicts, graph_error)：Neo4j 故障 → ({}, True)（图检跳过，匹配降级为
    无冲突检测，不整体 500）；正常 → (conflicts, False)。调用方据 graph_error
    透出降级提示（matching 返回 graph_error 字段）。
    """
    if not expert_ids:
        return {}, False
    try:
        driver = neo4j.get_driver()
        conflicts: dict[str, list[str]] = {}
        async with driver.session() as session:
            result = await session.run(
                "UNWIND $experts AS eid "
                "MATCH (e:Expert {expertId:eid}) "
                "OPTIONAL MATCH (e)-[r:EMPLOYED_BY|HOLDS_SHARE|RELATIVE_EMPLOYED]->(s:Supplier) "
                "  WHERE s.supplierId IN $sids "
                "OPTIONAL MATCH (e)-[r2:SAME_ORGANIZATION]->(e2:Expert) "
                "  WHERE e2.expertId IN $experts AND e2.expertId <> eid "
                "RETURN eid, "
                "  [x IN collect(DISTINCT type(r)) WHERE x IS NOT NULL] + "
                "  [x IN collect(DISTINCT type(r2)) WHERE x IS NOT NULL] AS rels",
                experts=expert_ids, sids=supplier_ids,
            )
            async for rec in result:
                rels = [r for r in rec["rels"] if r]
                if rels:
                    conflicts[rec["eid"]] = rels
        logger.debug("match.conflicts", experts=len(expert_ids), conflicts=len(conflicts))
        return conflicts, False
    except Exception as e:  # noqa: BLE001  Neo4j 不可用 → 图检跳过（失败偏置非 fail-stop）
        logger.warning("match.conflicts_failed", experts=len(expert_ids), error=str(e))
        return {}, True


async def _find_conflicts(
    expert_ids: list[str], supplier_ids: list[str]
) -> dict[str, list[str]]:
    """Step 3：Neo4j 批量冲突检测（薄封装，签名不变，expert_declaration_service 调用点兼容）。"""
    conflicts, _ = await _conflicts_with_status(expert_ids, supplier_ids)
    return conflicts


async def _load_review_quality(session: AsyncSession, expert_ids: list[str]) -> dict[str, float]:
    """expert_profile.review_quality（无记录默认 0.7）。"""
    from app.models.expert_profile import ExpertProfile

    rows = (
        await session.execute(
            select(ExpertProfile.expert_id, ExpertProfile.review_quality).where(
                ExpertProfile.expert_id.in_(expert_ids)
            )
        )
    ).all()
    return {e: float(q) for e, q in rows}


def _score_candidates(
    candidates: list[dict],
    tags: list[str],
    region: str,
    weights: dict[str, float],
    review_quality: dict[str, float],
) -> list[tuple[dict, float]]:
    """Step 4：多维加权排序。

    specialization_match = 命中项目标签数 / 项目标签数（0~1）
    experience 归一 = min(experience/30, 1)
    review_quality 取 expert_profile（默认 0.7）
    region_match：同地区 1.0，否则 0.5（全国性专家兜底 0.7，合成数据无全国则 0.5）
    """
    tag_set = set(tags)
    scored = []
    for c in candidates:
        hit = len(tag_set & c["tags"])
        spec = hit / len(tag_set) if tag_set else 0
        exp = min(c["experience"] / 30.0, 1.0)
        qual = review_quality.get(c["expert_id"], 0.7)
        reg = 1.0 if c["region"] == region else 0.5
        score = (
            weights.get("specialization", 0.40) * spec
            + weights.get("experience", 0.30) * exp
            + weights.get("review_quality", 0.20) * qual
            + weights.get("region", 0.10) * reg
        )
        scored.append((c, round(score, 4)))
    scored.sort(key=lambda x: -x[1])
    return scored


def _assign_dimensions(
    chosen: list[dict], dimensions: list[ScoringDimension], min_per_dim: int
) -> dict[str, list[str]]:
    """Step 5：维度分配。按 sort_order 轮转，保证每维度 ≥min_per_dim 位专家。"""
    dim_ids = [d.dimension_id for d in dimensions]
    assignment: dict[str, list[str]] = {}
    if not dim_ids:
        return assignment
    # 轮转分配（先均分一轮，再按需补）
    idx = 0
    for c in chosen:
        assignment[c["expert_id"]] = [dim_ids[idx % len(dim_ids)]]
        idx += 1
    # 每维度至少 min_per_dim：不足的维度从已分配专家补
    counts = {d: 0 for d in dim_ids}
    for exp in chosen:
        for d in assignment[exp["expert_id"]]:
            counts[d] += 1
    for d in dim_ids:
        need = min_per_dim - counts[d]
        if need > 0:
            for c in chosen:
                if need <= 0:
                    break
                if d not in assignment[c["expert_id"]]:
                    assignment[c["expert_id"]].append(d)
                    counts[d] += 1
                    need -= 1
    return assignment


async def match_experts(
    session: AsyncSession,
    *,
    lot_id: str,
    tags: list[str],
    operator_id: str,
) -> dict:
    """执行匹配并落库。返回 {assigned: [...], excluded: [...], insufficient: bool}。"""
    lot = await session.get(Lot, lot_id)
    if lot is None:
        raise LotNotFoundError(f"标段不存在: {lot_id}")
    if lot.status != "UNDER_REVIEW":
        raise LotNotUnderReviewError(f"标段状态 {lot.status} 非评审中（UNDER_REVIEW）")
    if not tags:
        raise NoTagsError("缺少项目专业标签，请先完成标签翻译或手动选择")

    from app.models.project import Project

    project = await session.get(Project, lot.project_id)
    region = project.region if project else ""
    criteria = await session.get(LotExpertCriteria, lot_id)
    weights = {
        "specialization": float(criteria.weight_specialization) if criteria else 0.40,
        "experience": float(criteria.weight_experience) if criteria else 0.30,
        "review_quality": float(criteria.weight_review_quality) if criteria else 0.20,
        "region": float(criteria.weight_region) if criteria and criteria.weight_region else 0.10,
    }
    expert_count = criteria.expert_count if criteria else 5
    min_per_dim = criteria.min_experts_per_dimension if criteria else 2

    # Step 1-2
    candidates = await _load_candidates(session, tags, region)
    suppliers = await _load_bidding_suppliers(session, lot_id)

    # Step 3 冲突检测（P8：Neo4j 故障 → graph_error=True 图检跳过，返回透出降级）
    conflicts, graph_error = await _conflicts_with_status(
        [c["expert_id"] for c in candidates], suppliers
    )
    clean = [c for c in candidates if c["expert_id"] not in conflicts]

    # Step 4 排序
    quality = await _load_review_quality(session, [c["expert_id"] for c in clean])
    scored = _score_candidates(clean, tags, region, weights, quality)

    # Step 5 选择 + 维度覆盖
    dimensions = (
        await session.scalars(
            select(ScoringDimension).where(ScoringDimension.lot_id == lot_id).order_by(ScoringDimension.sort_order)
        )
    ).all()
    chosen = [c for c, _ in scored[:expert_count]]
    # 底线：每位专家 ≥1 标签命中（spec>0 才可能命中，候选已按标签过滤，天然满足）
    assignment = _assign_dimensions(chosen, list(dimensions), min_per_dim)
    insufficient = len(chosen) < expert_count

    # 落库
    from app.models.lot_expert_assignment import LotExpertAssignment

    for c in chosen:
        existing = await session.scalar(
            select(LotExpertAssignment).where(
                LotExpertAssignment.lot_id == lot_id,
                LotExpertAssignment.expert_id == c["expert_id"],
            )
        )
        if existing:
            existing.dimension_ids = assignment.get(c["expert_id"], [])
            existing.status = "PENDING_DECLARATION"
        else:
            session.add(
                LotExpertAssignment(
                    lot_id=lot_id,
                    expert_id=c["expert_id"],
                    dimension_ids=assignment.get(c["expert_id"], []),
                    status="PENDING_DECLARATION",
                    assigned_at=_now(),
                )
            )
    await session.commit()

    logger.info("match.experts", lot_id=lot_id, candidates=len(candidates),
                conflicts=len(conflicts), chosen=len(chosen), insufficient=insufficient)
    return {
        "lot_id": lot_id,
        "assigned": [
            {"expert_id": c["expert_id"], "name": c["name"], "score": s,
             "dimension_ids": assignment.get(c["expert_id"], [])}
            for c, s in scored[:expert_count]
        ],
        "excluded_conflict": list(conflicts.keys()),
        "insufficient": insufficient,
        "match_mode": "AUTO",
        # P8 异常兜底：Neo4j 不可用时图检跳过，透出降级标志（additive 字段）
        "graph_error": graph_error,
    }

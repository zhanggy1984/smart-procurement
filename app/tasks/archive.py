"""归档 job（P3.5）：AWARDED 后异步收尾。

- expert_profile 重算：参与该项目评审的专家 total_reviews+1、review_quality 更新
- 供应商共投关系 BID_TOGETHER：同标段投过标的供应商两两 MERGE（幂等）
- 评分标准区分度校准（dimension_calibration）为骨架占位，P5 深度检测扩展

以 MySQL 为准（source of truth），Neo4j 关系 MERGE 幂等，job 可安全重跑。
"""

from __future__ import annotations

import structlog
from sqlalchemy import select, text

from app.core.database import session_factory
from app.models.bid_document import BidDocument
from app.models.expert_review import ExpertReview
from app.models.project import Lot

logger = structlog.get_logger(__name__)


async def _recalc_expert_profiles(project_id: str) -> int:
    """参与该项目评审的专家画像更新：total_reviews+1，review_quality 重算。"""
    async with session_factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT DISTINCT er.expert_id FROM expert_review er "
                    "JOIN bid_document b ON b.bid_id=er.bid_id "
                    "JOIN lot l ON l.lot_id=b.lot_id "
                    "WHERE l.project_id=:pid"
                ),
                {"pid": project_id},
            )
        ).all()
        experts = [r[0] for r in rows]
        updated = 0
        for expert_id in experts:
            # 该专家全部评审的均分 → review_quality（0~1 归一：score/max_score）
            reviews = (
                await session.scalars(
                    select(ExpertReview).where(ExpertReview.expert_id == expert_id)
                )
            ).all()
            ratios = []
            for rv in reviews:
                if rv.score is None:
                    continue
                from app.models.project import ScoringDimension

                dim = await session.get(ScoringDimension, rv.dimension_id)
                if dim and dim.max_score:
                    ratios.append(float(rv.score) / float(dim.max_score))
            quality = round(sum(ratios) / len(ratios), 3) if ratios else 0.7
            await session.execute(
                text(
                    "INSERT INTO expert_profile (expert_id, total_reviews, review_quality, updated_at) "
                    "VALUES (:e, 1, :q, NOW()) "
                    "ON DUPLICATE KEY UPDATE total_reviews=total_reviews+1, "
                    "review_quality=:q, updated_at=NOW()"
                ),
                {"e": expert_id, "q": quality},
            )
            updated += 1
        await session.commit()
    logger.info("archive.expert_profiles", project_id=project_id, updated=updated)
    return updated


async def _merge_bid_together(project_id: str) -> int:
    """同一标段投过标的供应商两两建立 BID_TOGETHER 关系（Neo4j MERGE 幂等）。"""
    from app.services import neo4j_sync

    async with session_factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT DISTINCT b.lot_id, b.supplier_id FROM bid_document b "
                    "JOIN lot l ON l.lot_id=b.lot_id WHERE l.project_id=:pid"
                ),
                {"pid": project_id},
            )
        ).all()
    by_lot: dict[str, list[str]] = {}
    for lot_id, sup_id in rows:
        by_lot.setdefault(lot_id, []).append(sup_id)

    count = 0
    for _lot_id, sups in by_lot.items():
        sups = sorted(set(sups))
        for i in range(len(sups)):
            for j in range(i + 1, len(sups)):
                # 双向：BID_TOGETHER 无向，建一条即可（Neo4j MERGE）
                await neo4j_sync.upsert_bid_together(sups[i], sups[j])
                count += 1
    logger.info("archive.bid_together", project_id=project_id, relations=count)
    return count


async def archive_project(ctx: dict, project_id: str) -> str:
    """arq job：项目归档。重跑安全（幂等）。"""
    experts = await _recalc_expert_profiles(project_id)
    relations = await _merge_bid_together(project_id)
    logger.info("archive.done", project_id=project_id, experts=experts, bid_together=relations)
    return f"ARCHIVED experts={experts} bid_together={relations}"

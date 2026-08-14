"""评审收尾（P3.5）。

- complete_review()：结束评审。校验 lot=UNDER_REVIEW + 该标段全部评审锁定
  （无 DRAFT）→ 生成报告 PDF → lot=EVALUATED
- submit_for_award()：推送定标。校验项目下全部 lot 已完成（EVALUATED/
  ABANDONED/DISQUALIFIED）→ project=AWARDED → 触发归档 job（arq）

报告内容：标段信息 + 各标书各维度得分汇总（reportlab 生成，中文 STSong-Light）。
围串标深度检测在 P5 接入（complete_review 预留，P3.5 先完成状态流转 + 报告）。
"""

from __future__ import annotations

import io
from datetime import datetime, timezone

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bid_document import BidDocument, BidStatus
from app.models.expert_review import ExpertReview, ReviewStatus
from app.models.project import Lot, Project, ScoringDimension
from app.models.supplier import Supplier
from app.services import review_service

logger = structlog.get_logger(__name__)

# 状态终值（P3.5 收尾）
_LOT_EVALUATED = "EVALUATED"
_LOT_FINAL = ("EVALUATED", "ABANDONED", "DISQUALIFIED")


class LotNotFoundError(ValueError):
    """标段不存在 → 404。"""


class ProjectNotFoundError(ValueError):
    """项目不存在 → 404。"""


class LotNotUnderReviewError(ValueError):
    """标段不在评审中 → 400。"""


class ReviewsIncompleteError(ValueError):
    """存在未完成（DRAFT）评审 → 400。"""


class ProjectNotReadyError(ValueError):
    """项目下存在未完成标段 → 400。"""


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _build_report_pdf(session: AsyncSession, lot: Lot) -> bytes:
    """生成评审总结报告 PDF（reportlab 表格：各标书 × 各维度得分）。"""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4)
    styles = getSampleStyleSheet()
    title = Paragraph(f"标段评审总结报告：{lot.name}", styles["Title"])

    # 各 FROZEN 标书 → 各维度得分
    bids = (
        await session.scalars(
            select(BidDocument).where(BidDocument.lot_id == lot.lot_id, BidDocument.status == BidStatus.FROZEN)
        )
    ).all()
    data = [["供应商标书", "维度", "得分", "状态"]]
    for bid in bids:
        reviews = (
            await session.scalars(
                select(ExpertReview).where(ExpertReview.bid_id == bid.bid_id)
            )
        ).all()
        if not reviews:
            data.append([bid.bid_id, "-", "-", "无评审"])
            continue
        for r in reviews:
            data.append([bid.bid_id, r.dimension_id, str(r.score or "-"), r.status])
    table = Table(data, colWidths=[50 * mm, 45 * mm, 30 * mm, 35 * mm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EEEEEE")),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("FONTNAME", (0, 0), (-1, -1), "STSong-Light"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
    ]))
    doc.build([title, Spacer(1, 6 * mm), table])
    return buf.getvalue()


async def complete_review(
    session: AsyncSession,
    *,
    lot_id: str,
    operator_id: str,
) -> dict:
    """结束评审：校验 → 生成报告 → lot=EVALUATED。返回报告 PDF（bytes）与汇总。"""
    lot = await session.get(Lot, lot_id)
    if lot is None:
        raise LotNotFoundError(f"标段不存在: {lot_id}")
    if lot.status != "UNDER_REVIEW":
        raise LotNotUnderReviewError(f"标段状态 {lot.status} 非评审中（UNDER_REVIEW）")

    bids = (
        await session.scalars(
            select(BidDocument).where(BidDocument.lot_id == lot_id, BidDocument.status == BidStatus.FROZEN)
        )
    ).all()
    for bid in bids:
        reviews = (
            await session.scalars(select(ExpertReview).where(ExpertReview.bid_id == bid.bid_id))
        ).all()
        if any(r.status == ReviewStatus.DRAFT for r in reviews):
            raise ReviewsIncompleteError(f"标书 {bid.bid_id} 存在未完成（DRAFT）评审，无法结束")

    report_pdf = await _build_report_pdf(session, lot)
    lot.status = _LOT_EVALUATED
    lot.updated_at = _now()
    await session.commit()
    logger.info("lot.complete_review", lot_id=lot_id, operator=operator_id, bids=len(bids))
    return {"lot_id": lot_id, "status": lot.status, "report_pdf": report_pdf}


async def get_lot_summary(session: AsyncSession, *, lot_id: str) -> dict:
    """评标汇总（P6.3 评标汇总页）。

    各 FROZEN 标书 × 各维度已提交评审得分（CONFIRMED/MANUAL_ADJUSTED，SUSPENDED 不计；
    同维度多评审取平均）。综合得分归一化百分制：
        100 × Σ(weight_i × score_i / max_score_i)
    因 Σweight=1.0、ΣmaxScore=100，维度全满分时综合得分=100（与 solution.md 校验自洽）。
    排名按综合得分降序，同分按报价低者优先。
    """
    lot = await session.get(Lot, lot_id)
    if lot is None:
        raise LotNotFoundError(f"标段不存在: {lot_id}")
    project = await session.get(Project, lot.project_id)

    dims = (
        await session.scalars(
            select(ScoringDimension)
            .where(ScoringDimension.lot_id == lot_id)
            .order_by(ScoringDimension.sort_order)
        )
    ).all()

    rows = (
        await session.execute(
            select(BidDocument, Supplier)
            .join(Supplier, BidDocument.supplier_id == Supplier.supplier_id)
            .where(BidDocument.lot_id == lot_id, BidDocument.status == BidStatus.FROZEN)
            .order_by(BidDocument.bid_amount)
        )
    ).all()
    bid_ids = [bid.bid_id for bid, _ in rows]
    reviews = (
        await session.scalars(select(ExpertReview).where(ExpertReview.bid_id.in_(bid_ids)))
    ).all() if bid_ids else []

    def _fmt(v):
        return float(v) if v is not None else None

    bids = []
    for bid, supplier in rows:
        dim_scores = []
        for d in dims:
            scores = [
                r.score for r in reviews
                if r.bid_id == bid.bid_id
                and r.dimension_id == d.dimension_id
                and r.status in (ReviewStatus.CONFIRMED, ReviewStatus.MANUAL_ADJUSTED)
                and r.score is not None
            ]
            dim_scores.append({
                "dimension_id": d.dimension_id,
                "name": d.name,
                "max_score": _fmt(d.max_score),
                "weight": _fmt(d.weight),
                "score": round(float(sum(scores)) / len(scores), 2) if scores else None,
                "status": "LOCKED" if scores else "PENDING",
            })
        # 综合得分 = 100 × Σ(weight × score/max_score)，仅统计已锁定维度
        weighted = sum(
            (ds["score"] / ds["max_score"]) * ds["weight"]
            for ds in dim_scores
            if ds["score"] is not None and ds["max_score"]
        )
        bids.append({
            "bid_id": bid.bid_id,
            "supplier_id": supplier.supplier_id,
            "supplier_name": supplier.name,
            "bid_amount": _fmt(bid.bid_amount),
            "duration": bid.duration,
            "team_size": bid.team_size,
            "status": bid.status,
            "dimension_scores": dim_scores,
            "weighted_total": round(weighted * 100, 2),
        })

    bids.sort(key=lambda b: (-b["weighted_total"], b["bid_amount"] or 0))
    for i, b in enumerate(bids, 1):
        b["rank"] = i

    return {
        "lot": {
            "lot_id": lot.lot_id,
            "lot_code": lot.lot_code,
            "name": lot.name,
            "status": lot.status,
            "budget": _fmt(lot.budget),
            "project_id": lot.project_id,
            "project_code": project.project_code if project else None,
            "project_name": project.name if project else None,
        },
        "dimensions": [
            {
                "dimension_id": d.dimension_id,
                "name": d.name,
                "max_score": _fmt(d.max_score),
                "weight": _fmt(d.weight),
            }
            for d in dims
        ],
        "bids": bids,
    }


async def submit_for_award(
    session: AsyncSession,
    *,
    project_id: str,
    operator_id: str,
) -> dict:
    """推送定标：校验项目下全部 lot 终态 → project=AWARDED → 触发归档 job。"""
    project = await session.get(Project, project_id)
    if project is None:
        raise ProjectNotFoundError(f"项目不存在: {project_id}")

    lots = (await session.scalars(select(Lot).where(Lot.project_id == project_id))).all()
    unfinished = [l.lot_id for l in lots if l.status not in _LOT_FINAL]
    if unfinished:
        raise ProjectNotReadyError(f"项目存在未完成标段: {unfinished}")

    project.status = "AWARDED"
    project.updated_at = _now()
    await session.commit()

    # 触发归档 job（fire-and-forget）
    from app.tasks import dispatch

    await dispatch.enqueue_archive(project_id)
    logger.info("project.submit_for_award", project_id=project_id, operator=operator_id)
    return {"project_id": project_id, "status": project.status}

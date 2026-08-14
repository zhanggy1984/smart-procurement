"""评审收尾 API（P3.5）。

- POST /lots/{id}/complete-review      结束评审 → 报告 → lot=EVALUATED
- GET  /lots/{id}/summary/report        下载评审总结报告 PDF
- POST /projects/{id}/submit-for-award  推送定标 → project=AWARDED → 归档 job

权限：complete-review / submit-for-award 限 PM/ADMIN；报告下载 PM/ADMIN。
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_roles
from app.core.database import get_db_session
from app.models.user import Role, User
from app.models.project import Lot
from app.services import closeout_service as svc
from app.services import fraud_detection_service as fraud

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["closeouts"])


def _service_to_http(exc: Exception) -> HTTPException:
    mapping = {
        svc.LotNotFoundError: status.HTTP_404_NOT_FOUND,
        svc.ProjectNotFoundError: status.HTTP_404_NOT_FOUND,
        svc.LotNotUnderReviewError: status.HTTP_400_BAD_REQUEST,
        svc.ReviewsIncompleteError: status.HTTP_400_BAD_REQUEST,
        svc.ProjectNotReadyError: status.HTTP_400_BAD_REQUEST,
        fraud.LotNotFoundError: status.HTTP_404_NOT_FOUND,
        fraud.LotNotBiddableError: status.HTTP_400_BAD_REQUEST,
        fraud.NoValidBidsError: status.HTTP_400_BAD_REQUEST,
        fraud.LotNotPrescreenError: status.HTTP_400_BAD_REQUEST,
        fraud.BidNotInLotError: status.HTTP_400_BAD_REQUEST,
    }
    return HTTPException(mapping.get(type(exc), status.HTTP_422_UNPROCESSABLE_ENTITY), detail=str(exc))


@router.post("/lots/{lot_id}/complete-review", summary="结束评审（→ EVALUATED + 生成报告）")
async def complete_review(
    lot_id: str,
    session: AsyncSession = Depends(get_db_session),
    user: User = Depends(require_roles(Role.PROJECT_MANAGER, Role.ADMIN)),
) -> dict:
    logger.debug("closeout.complete_request", operator=user.user_id, lot_id=lot_id)
    try:
        result = await svc.complete_review(session, lot_id=lot_id, operator_id=user.user_id)
    except (svc.LotNotFoundError, svc.LotNotUnderReviewError, svc.ReviewsIncompleteError) as e:
        raise _service_to_http(e)
    logger.info("closeout.complete_success", lot_id=lot_id, status=result["status"])
    return {
        "lot_id": lot_id,
        "status": result["status"],
        "report_url": f"/api/v1/lots/{lot_id}/summary/report",
    }


@router.post("/lots/{lot_id}/close-bidding", summary="关闭投标（初筛三检 → 风险评分）")
async def close_bidding(
    lot_id: str,
    session: AsyncSession = Depends(get_db_session),
    user: User = Depends(require_roles(Role.PROJECT_MANAGER, Role.ADMIN)),
) -> dict:
    logger.debug("fraud.close_request", operator=user.user_id, lot_id=lot_id)
    try:
        result = await fraud.close_bidding(session, lot_id=lot_id, operator_id=user.user_id)
    except (fraud.LotNotFoundError, fraud.LotNotBiddableError, fraud.NoValidBidsError) as e:
        raise _service_to_http(e)
    logger.info("fraud.close_done", lot_id=lot_id, risk=result["risk"], score=result["total_score"])
    return result


@router.post("/lots/{lot_id}/confirm-prescreen", summary="PM 确认放行初筛待办（深度检测 → 放行/建议废标）")
async def confirm_prescreen(
    lot_id: str,
    session: AsyncSession = Depends(get_db_session),
    user: User = Depends(require_roles(Role.PROJECT_MANAGER, Role.ADMIN)),
) -> dict:
    """P5.3 闭环：PRE_SCREEN → 深度检测（text+graph+price）→ LOW/MEDIUM 放行进入评审；
    HIGH/CRITICAL 不放行，前端提示废标建议。"""
    logger.debug("fraud.prescreen_request", operator=user.user_id, lot_id=lot_id)
    try:
        result = await fraud.confirm_prescreen(session, lot_id=lot_id, operator_id=user.user_id)
    except (fraud.LotNotFoundError, fraud.LotNotPrescreenError) as e:
        raise _service_to_http(e)
    logger.info("fraud.prescreen_done", lot_id=lot_id, risk=result["risk"], released=result["released"])
    return result


@router.post("/lots/{lot_id}/bids/{bid_id}/disqualify", summary="PM 废标（标书 → DISQUALIFIED）")
async def disqualify_bid(
    lot_id: str,
    bid_id: str,
    session: AsyncSession = Depends(get_db_session),
    user: User = Depends(require_roles(Role.PROJECT_MANAGER, Role.ADMIN)),
) -> dict:
    """E2E-3 闭环：初筛待办/评审中标书标记废标（黑名单级联亦走 DISQUALIFIED，此处为人工操作）。"""
    logger.debug("fraud.disqualify_request", operator=user.user_id, lot_id=lot_id, bid_id=bid_id)
    try:
        result = await fraud.disqualify_bid(session, lot_id=lot_id, bid_id=bid_id, operator_id=user.user_id)
    except (fraud.LotNotFoundError, fraud.LotNotPrescreenError, fraud.BidNotInLotError) as e:
        raise _service_to_http(e)
    logger.info("fraud.disqualify_done", lot_id=lot_id, bid_id=bid_id)
    return result


@router.get("/lots/{lot_id}/summary", summary="评标汇总（各标书各维度得分 + 加权总分 + 排名）")
async def lot_summary(
    lot_id: str,
    session: AsyncSession = Depends(get_db_session),
    user: User = Depends(require_roles(Role.PROJECT_MANAGER, Role.ADMIN, Role.REVIEW_EXPERT)),
) -> dict:
    logger.debug("closeout.summary_request", operator=user.user_id, lot_id=lot_id)
    try:
        return await svc.get_lot_summary(session, lot_id=lot_id)
    except svc.LotNotFoundError as e:
        raise _service_to_http(e)


@router.get("/lots/{lot_id}/summary/report", summary="下载评审总结报告 PDF")
async def download_report(
    lot_id: str,
    session: AsyncSession = Depends(get_db_session),
    user: User = Depends(require_roles(Role.PROJECT_MANAGER, Role.ADMIN, Role.REVIEW_EXPERT)),
) -> Response:
    lot = await session.get(Lot, lot_id)
    if lot is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"标段不存在: {lot_id}")
    pdf = await svc._build_report_pdf(session, lot)
    filename = f"lot_{lot_id}_report.pdf"
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/projects/{project_id}/submit-for-award", summary="推送定标（→ AWARDED + 归档）")
async def submit_for_award(
    project_id: str,
    session: AsyncSession = Depends(get_db_session),
    user: User = Depends(require_roles(Role.PROJECT_MANAGER, Role.ADMIN)),
) -> dict:
    logger.debug("closeout.award_request", operator=user.user_id, project_id=project_id)
    try:
        result = await svc.submit_for_award(session, project_id=project_id, operator_id=user.user_id)
    except (svc.ProjectNotFoundError, svc.ProjectNotReadyError) as e:
        raise _service_to_http(e)
    logger.info("closeout.award_success", project_id=project_id, status=result["status"])
    return {"project_id": project_id, "status": result["status"]}

"""专家匹配 API（P4.2）。

- POST /lots/{id}/match-experts  执行 5 步匹配（校验 lot=UNDER_REVIEW）→ 落库
- GET  /lots/{id}/match-experts  查看匹配结果（assignment + 维度分配）

权限：PM/ADMIN。
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_roles
from app.core.database import get_db_session
from app.models.lot_expert_assignment import LotExpertAssignment
from app.models.user import Role, User
from app.services import expert_match_service as svc

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["matching"])


class MatchRequest(BaseModel):
    """匹配请求：项目专业标签（P4.1 翻译产物或 PM 手动选择）。"""

    tags: list[str] = Field(..., min_length=1, description="专业标签（受控词表内）")


def _service_to_http(exc: Exception) -> HTTPException:
    mapping = {
        svc.LotNotFoundError: status.HTTP_404_NOT_FOUND,
        svc.LotNotUnderReviewError: status.HTTP_400_BAD_REQUEST,
        svc.NoTagsError: status.HTTP_400_BAD_REQUEST,
    }
    return HTTPException(mapping.get(type(exc), status.HTTP_422_UNPROCESSABLE_ENTITY), detail=str(exc))


@router.post("/lots/{lot_id}/match-experts", summary="执行专家匹配并落库")
async def match_experts(
    lot_id: str,
    body: MatchRequest,
    session: AsyncSession = Depends(get_db_session),
    user: User = Depends(require_roles(Role.PROJECT_MANAGER, Role.ADMIN)),
) -> dict:
    logger.debug("match.request", operator=user.user_id, lot_id=lot_id, tags=body.tags)
    try:
        result = await svc.match_experts(
            session, lot_id=lot_id, tags=body.tags, operator_id=user.user_id
        )
    except (svc.LotNotFoundError, svc.LotNotUnderReviewError, svc.NoTagsError) as e:
        raise _service_to_http(e)
    logger.info("match.done", lot_id=lot_id, assigned=len(result["assigned"]),
                conflicts=len(result["excluded_conflict"]), insufficient=result["insufficient"])
    return result


@router.get("/lots/{lot_id}/match-experts", summary="查看匹配结果")
async def get_match_results(
    lot_id: str,
    session: AsyncSession = Depends(get_db_session),
    user: User = Depends(require_roles(Role.PROJECT_MANAGER, Role.ADMIN, Role.REVIEW_EXPERT)),
) -> dict:
    assignments = (
        await session.scalars(
            select(LotExpertAssignment).where(LotExpertAssignment.lot_id == lot_id)
        )
    ).all()
    return {
        "lot_id": lot_id,
        "assigned": [
            {"expert_id": a.expert_id, "dimension_ids": a.dimension_ids or [],
             "status": a.status}
            for a in assignments
        ],
    }

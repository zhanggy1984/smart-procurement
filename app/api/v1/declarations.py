"""专家回避申报 API（P4.3）。

- GET  /experts/me/assignments          我的任务列表
- GET  /experts/assignments/{id}/declaration  待申报供应商
- POST /experts/assignments/{id}/declare      提交回避申报

权限：REVIEW_EXPERT（本人）。
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_roles
from app.core.database import get_db_session
from app.models.expert import Expert
from app.models.user import Role, User
from app.services import expert_declaration_service as svc

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["declarations"])


class DeclarationItem(BaseModel):
    """单个供应商的确认/申报。"""

    supplier_id: str = Field(..., description="供应商 ID")
    has_conflict: bool = Field(False, description="是否申报与该供应商有回避冲突")
    relation_type: str | None = Field(None, description="冲突关系类型（EMPLOYED_BY/HOLDS_SHARE 等）")
    relation_detail: str | None = Field(None, description="冲突详情（如'曾任该公司技术总监'）")


class DeclareRequest(BaseModel):
    """回避申报请求。"""

    confirmations: list[DeclarationItem] = Field(..., description="全部投标供应商的确认/申报列表")


async def _resolve_expert(session: AsyncSession, user: User) -> str:
    """登录账号 → 专家实体（display_name=专家名，P4.2 同款反查）。"""
    experts = (
        await session.scalars(select(Expert).where(Expert.name == user.display_name))
    ).all()
    if not experts:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="当前账号未关联评审专家档案")
    return experts[0].expert_id


def _service_to_http(exc: Exception) -> HTTPException:
    mapping = {
        svc.AssignmentNotFoundError: status.HTTP_404_NOT_FOUND,
        svc.AssignmentAccessDeniedError: status.HTTP_403_FORBIDDEN,
        svc.AlreadyDeclaredError: status.HTTP_409_CONFLICT,
        svc.NoConflictSupplierError: status.HTTP_400_BAD_REQUEST,
    }
    return HTTPException(mapping.get(type(exc), status.HTTP_422_UNPROCESSABLE_ENTITY), detail=str(exc))


@router.get("/experts/me/assignments", summary="我的任务列表")
async def my_assignments(
    session: AsyncSession = Depends(get_db_session),
    user: User = Depends(require_roles(Role.REVIEW_EXPERT)),
) -> dict:
    expert_id = await _resolve_expert(session, user)
    assignments = await svc.list_my_assignments(session, expert_id)
    logger.info("declaration.my_assignments", expert_id=expert_id, count=len(assignments))
    return {"assignments": assignments}


@router.get("/experts/assignments/{assignment_id}/declaration", summary="待申报供应商")
async def get_declaration(
    assignment_id: int,
    session: AsyncSession = Depends(get_db_session),
    user: User = Depends(require_roles(Role.REVIEW_EXPERT)),
) -> dict:
    expert_id = await _resolve_expert(session, user)
    try:
        return await svc.get_declaration(session, assignment_id=assignment_id, expert_id=expert_id)
    except (svc.AssignmentNotFoundError, svc.AssignmentAccessDeniedError) as e:
        raise _service_to_http(e)


@router.post("/experts/assignments/{assignment_id}/declare", summary="提交回避申报")
async def declare(
    assignment_id: int,
    body: DeclareRequest,
    session: AsyncSession = Depends(get_db_session),
    user: User = Depends(require_roles(Role.REVIEW_EXPERT)),
) -> dict:
    expert_id = await _resolve_expert(session, user)
    try:
        return await svc.declare(
            session,
            assignment_id=assignment_id,
            expert_id=expert_id,
            confirmations=[c.model_dump() for c in body.confirmations],
        )
    except (svc.AssignmentNotFoundError, svc.AssignmentAccessDeniedError,
            svc.AlreadyDeclaredError, svc.NoConflictSupplierError) as e:
        raise _service_to_http(e)

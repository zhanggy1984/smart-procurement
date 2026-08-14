"""用户管理 API（P6.2 管理员端用户管理页）。

- GET  /users                分页列表（keyword 模糊匹配 username/display_name）
- POST /users                创建用户（密码复杂度校验 + 用户名冲突 409）
- PUT  /users/{id}/status    启用/禁用 / 改角色

全部限 ADMIN。
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_roles
from app.core.database import get_db_session
from app.models.user import Role, User
from app.schemas.user import UserCreate, UserOut, UserStatusUpdate
from app.services import user_service as svc

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["users"])


@router.get("/users", summary="用户列表（分页）")
async def list_users(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    keyword: str | None = Query(None, description="模糊匹配用户名/姓名"),
    session: AsyncSession = Depends(get_db_session),
    _operator: User = Depends(require_roles(Role.ADMIN)),
) -> dict:
    users, total = await svc.list_users(
        session, page=page, page_size=page_size, keyword=keyword
    )
    logger.debug("user.list", operator=_operator.user_id, total=total, page=page)
    return {
        "items": [UserOut.model_validate(u).model_dump() for u in users],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.post(
    "/users",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
    summary="创建用户",
)
async def create_user(
    body: UserCreate,
    session: AsyncSession = Depends(get_db_session),
    operator: User = Depends(require_roles(Role.ADMIN)),
) -> UserOut:
    logger.debug("user.create_request", operator=operator.user_id, username=body.username)
    try:
        user = await svc.create_user(session, **body.model_dump())
    except svc.UsernameTakenError as e:
        logger.info("user.create_conflict", error=str(e))
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(e))
    logger.info("user.create_success", user_id=user.user_id, role=user.role)
    return UserOut.model_validate(user)


@router.put(
    "/users/{user_id}/status",
    response_model=UserOut,
    summary="启用/禁用/改角色",
)
async def update_user_status(
    user_id: str,
    body: UserStatusUpdate,
    session: AsyncSession = Depends(get_db_session),
    operator: User = Depends(require_roles(Role.ADMIN)),
) -> UserOut:
    logger.debug(
        "user.status_request",
        operator=operator.user_id,
        user_id=user_id,
        is_active=body.is_active,
        role=body.role,
    )
    try:
        user = await svc.update_user(
            session, user_id, is_active=body.is_active, role=body.role
        )
    except svc.InvalidRoleError as e:
        logger.info("user.status_invalid_role", error=str(e))
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"用户不存在: {user_id}")
    logger.info("user.status_success", user_id=user_id)
    return UserOut.model_validate(user)

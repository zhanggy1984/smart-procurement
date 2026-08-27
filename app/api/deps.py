"""API 依赖：JWT 鉴权 + 角色控制（P1.2）。

get_current_user：解析 Authorization: Bearer <access_token> → 查库校验
用户仍存在且 active（禁用后 token 立即失效，而非等过期）。
require_roles：RBAC，当前用户角色不在白名单 → 403。
"""

from __future__ import annotations

from typing import Awaitable, Callable

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import security
from app.core.database import get_db_session
from app.models.user import User
from app.services import user_service

# auto_error=False：无凭证时不抛自动 401，由本模块统一返回中文错误
_bearer = HTTPBearer(auto_error=False)


async def _resolve_current_user(
    credentials: HTTPAuthorizationCredentials | None,
    session: AsyncSession,
    *,
    check_must_change: bool,
) -> User:
    """JWT 解析当前用户（有效/存在/active）。check_must_change=True 时首登强改 403。"""
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未提供认证凭证",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        user_id = security.get_token_subject(credentials.credentials, security.TOKEN_TYPE_ACCESS)
    except jwt.PyJWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="登录凭证无效或已过期",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = await user_service.get_user(session, user_id)
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户不存在或已禁用",
        )
    # 自查 #6：首登强改——未改密账号除改密端点外业务 API 一律 403
    if check_must_change and getattr(user, "must_change_password", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="首次登录必须修改初始密码",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: AsyncSession = Depends(get_db_session),
) -> User:
    """从 access_token 解析当前用户。无效/过期/禁用 401；首登未改密 403。"""
    return await _resolve_current_user(credentials, session, check_must_change=True)


async def get_current_user_allow_change_password(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: AsyncSession = Depends(get_db_session),
) -> User:
    """改密端点专用：鉴权但不校验首登强改（否则未改密账号进不来改密）。"""
    return await _resolve_current_user(credentials, session, check_must_change=False)


def require_roles(*roles: str) -> Callable[..., Awaitable[User]]:
    """角色鉴权依赖工厂：require_roles(Role.ADMIN, Role.PROJECT_MANAGER)。

    返回依赖函数，注入后校验当前用户角色，不在白名单 → 403。
    """

    async def _checker(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"需要角色: {'/'.join(roles)}",
            )
        return user

    return _checker

"""认证 API（P1.2）：登录签发 JWT + refresh 换新。

设计（solution.md 4 核心 API）：
- POST /api/v1/auth/login：username+password → 200 + JWT（access 30min + refresh 7d）
- POST /api/v1/auth/refresh：refresh_token → 新 access_token
错误密码 / 账号不存在统一 401（防枚举）。
日志：入参密码脱敏，出参不记 token 本体（solution.md 日志规范）。
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import security
from app.core.crypto import redact
from app.core.database import get_db_session
from app.services import user_service

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


# ==================== 请求/响应模型 ====================
class LoginRequest(BaseModel):
    """登录请求。password 字段禁止在日志中明文输出。"""

    username: str
    password: str


class RefreshRequest(BaseModel):
    """用 refresh_token 换新 access_token。"""

    refresh_token: str


class UserOut(BaseModel):
    """登录返回的用户信息（不含 password_hash）。"""

    model_config = ConfigDict(from_attributes=True)

    user_id: str
    username: str
    role: str
    display_name: str
    email: str | None = None
    phone: str | None = None


class TokenResponse(BaseModel):
    """登录成功返回的令牌。"""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: UserOut


class AccessTokenResponse(BaseModel):
    """refresh 换新返回。"""

    access_token: str
    token_type: str = "bearer"


# ==================== 端点 ====================
@router.post("/login", response_model=TokenResponse, summary="登录")
async def login(
    req: LoginRequest,
    session: AsyncSession = Depends(get_db_session),
) -> TokenResponse:
    logger.debug(
        "auth.login_request",
        username=req.username,
        password=redact(req.password),
    )
    user = await user_service.authenticate(session, req.username, req.password)
    if user is None:
        logger.info("auth.login_failed", username=req.username)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
        )

    tokens = TokenResponse(
        access_token=security.create_access_token(user.user_id),
        refresh_token=security.create_refresh_token(user.user_id),
        user=UserOut.model_validate(user),
    )
    logger.info(
        "auth.login_success",
        user_id=user.user_id,
        role=user.role,
    )
    return tokens


@router.post("/refresh", response_model=AccessTokenResponse, summary="刷新 access_token")
async def refresh(
    req: RefreshRequest,
    session: AsyncSession = Depends(get_db_session),
) -> AccessTokenResponse:
    # refresh_token 本体敏感，不入日志
    logger.debug("auth.refresh_request", has_refresh_token=bool(req.refresh_token))
    try:
        user_id = security.get_token_subject(req.refresh_token, security.TOKEN_TYPE_REFRESH)
    except Exception:  # noqa: BLE001  统一按无效处理（jwt 各类异常）
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="刷新令牌无效或已过期",
        )

    user = await user_service.get_user(session, user_id)
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户不存在或已禁用",
        )
    logger.info("auth.refresh_success", user_id=user_id)
    return AccessTokenResponse(access_token=security.create_access_token(user.user_id))

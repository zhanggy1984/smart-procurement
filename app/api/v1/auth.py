"""认证 API（P1.2）：登录签发 JWT + refresh 换新 + 改密。

设计（solution.md 4 核心 API）：
- POST /api/v1/auth/login：username+password → 200 + JWT（access 30min + refresh 7d）
- POST /api/v1/auth/refresh：refresh_token → 新 access_token + 新 refresh_token（轮换）
- POST /api/v1/auth/change-password：改密并清除首登强改标记

自查 #6 安全加固（对齐 solution.md S4/S5）：
- 登录限流（S4）：IP 级限流，每 IP 每 60s 允许 5 次登录尝试，第 6 次 → 429 +
  15min 冷却；冷却期间一律 429；Redis 挂 fail-open
- refresh 轮换（S5）：JWT 带 jti，签发入 user-scoped 白名单；refresh 时 SET GET
  原子置 USED 标记消费 + 发新 access/refresh；旧 refresh 二次使用（复用信号）→
  撤销该用户全部 refresh token → 401；签发时 Redis 挂未登记 / 键空间丢失 →
  fail-open 放行（退回纯 JWT 可用），不误判为复用
- 首登强改：must_change_password=True 账号业务 API 403（改密端点豁免），改密后恢复

错误密码 / 账号不存在统一 401（防枚举）。
日志：入参密码脱敏，出参不记 token 本体（solution.md 日志规范）。
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user_allow_change_password
from app.core import security
from app.core.config import settings
from app.core.crypto import redact
from app.core.database import get_db_session
from app.core.redis import get_redis, redis_warn_once
from app.models.user import User
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


class ChangePasswordRequest(BaseModel):
    """修改密码（旧密码 + 新密码）。新密码复杂度由 service 校验。"""

    old_password: str
    new_password: str


class UserOut(BaseModel):
    """登录返回的用户信息（不含 password_hash）。"""

    model_config = ConfigDict(from_attributes=True)

    user_id: str
    username: str
    role: str
    display_name: str
    email: str | None = None
    phone: str | None = None
    must_change_password: bool = False


class TokenResponse(BaseModel):
    """登录成功返回的令牌。"""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: UserOut


class AccessTokenResponse(BaseModel):
    """refresh 换新返回（轮换：同时返还新 refresh_token，前端需更新存储）。"""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"


# ==================== 登录限流（Redis 固定窗口，自查 #6 / solution.md S4） ====================
# key 语义：auth:ip:{ip}:req 登录尝试计数（60s 固定窗口）；auth:ip:{ip}:cooldown 冷却标记。
# S4 规定 IP 级限流（5 次/min，超出 429 + 15min 冷却）。不用账号锁定——锁定账号
# 引入 DoS 向量（攻击者可故意失败 5 次锁死他人账号），IP 级只惩罚攻击者自己的 IP。
# Redis 挂一律 fail-open（不阻断登录），对齐现有降级哲学。


def _client_ip(request: Request) -> str:
    """取客户端真实 IP：优先 nginx 覆盖式写入的 X-Real-IP，否则 TCP 对端。

    信任边界（自查 #6 复查）：nginx（docker/nginx/nginx.conf）用
    `proxy_set_header X-Real-IP $remote_addr` **覆盖式**写入真实客户端 IP——客户端
    自带的 X-Real-IP 会被替换，伪造无效。**绝不可信 X-Forwarded-For 首跳**：nginx 用
    `$proxy_add_x_forwarded_for` 是"追加"真实 IP 到末位，首跳由客户端自定，攻击者可
    逐请求轮换伪造 IP 完全绕过限流，或塞入受害者 IP 触发冷却造成登录 DoS。
    直连后端（宿主 18002 调试/评测绕过网关，见 README 对外链路）时 X-Real-IP 缺失
    → 回退 request.client.host（TCP 对端 IP，不可伪造）。残余风险：若 18002 被公网
    可达，直连攻击者可自行伪造 X-Real-IP——该端口定位仅限开发调试，生产只走网关。
    """
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    return request.client.host if request.client else "unknown"


async def _check_login_limited(ip: str) -> bool:
    """登录前置：该 IP 是否处于冷却中。Redis 挂 → 不限制。"""
    try:
        r = get_redis()
        return await r.get(f"auth:ip:{ip}:cooldown") is not None
    except Exception as e:  # noqa: BLE001
        await redis_warn_once("auth.redis_down", str(e))
        return False


async def _record_login_attempt(ip: str) -> bool:
    """记录一次登录尝试；超阈值（第 6 次）→ 设冷却并返回 True（应 429）。

    Redis 挂 → 返回 False（不拦截），fail-open。
    """
    try:
        r = get_redis()
        key = f"auth:ip:{ip}:req"
        pipe = r.pipeline()
        pipe.incr(key)
        pipe.expire(key, settings.login_rate_window_seconds)
        count, _ = await pipe.execute()
        if count > settings.login_rate_limit:
            await r.set(f"auth:ip:{ip}:cooldown", "1", ex=settings.login_cooldown_seconds)
            return True
        return False
    except Exception as e:  # noqa: BLE001
        await redis_warn_once("auth.redis_down", str(e))
        return False


async def _clear_login_attempts(ip: str) -> None:
    """登录成功：清该 IP 窗口计数。Redis 挂 → 忽略。"""
    try:
        r = get_redis()
        await r.delete(f"auth:ip:{ip}:req")
    except Exception as e:  # noqa: BLE001
        await redis_warn_once("auth.redis_down", str(e))


# ==================== refresh 轮换白名单（自查 #6 / solution.md S5） ====================
# key 语义：
#   refresh:user:{user_id}:{jti} → "1"（在册未消费）/ "USED"（已消费，复用信号）
#   refresh:revoke:{user_id}      → "1"（用户级全量吊销标记）
# 签发入白名单；refresh 时 `SET key USED GET` 原子置标记消费（返回旧值，杜绝
# GET+DEL 双读双删竞态）。复用检测用「旧值 == USED」而非「键缺失」——键缺失既可能
# 是复用（消费后键仍留在册置 USED），也可能是签发时 Redis 挂未登记 / 键空间丢失
# （fail-open 场景），两者语义相反，必须区分：
#   - 旧值 USED  → 复用信号 → 撤销该用户全部 refresh token（S5 要求）
#   - 键缺失且无全量吊销标记 → 未登记 → fail-open 放行（退回纯 JWT 可用）
#   - 键缺失但有全量吊销标记 → 被吊销 → 401（吊销必须生效，不能因键缺失误放行）
# Redis 挂 fail-open：签发跳过登记；refresh 时跳过白名单校验（退回纯 JWT 可用）。
_REFRESH_WHITELIST_PREFIX = "refresh:user:"
_REFRESH_CONSUMED = "USED"
_REFRESH_REVOKED_PREFIX = "refresh:revoke:"


async def _register_refresh_jti(token: str, user_id: str) -> None:
    """登记 refresh_token 白名单（TTL=refresh 有效期）。Redis 挂 → 忽略。"""
    try:
        jti = security.get_token_jti(token, security.TOKEN_TYPE_REFRESH)
        r = get_redis()
        await r.set(
            f"{_REFRESH_WHITELIST_PREFIX}{user_id}:{jti}",
            "1",
            ex=settings.jwt_refresh_token_expire_days * 86400,
        )
    except Exception as e:  # noqa: BLE001
        await redis_warn_once("auth.redis_down", str(e))


async def _revoke_all_refresh(user_id: str) -> None:
    """撤销用户全部 refresh token（S5 复用检测：泄露信号后全量吊销）。

    设全量吊销标记（区分「被吊销」与「未登记」，吊销后键缺失必须 401）+ 清白名单。
    Redis 挂 → 忽略（fail-open，吊销降级）。
    """
    try:
        r = get_redis()
        await r.set(
            f"{_REFRESH_REVOKED_PREFIX}{user_id}",
            "1",
            ex=settings.jwt_refresh_token_expire_days * 86400,
        )
        cursor = 0
        while True:
            cursor, keys = await r.scan(cursor=cursor, match=f"{_REFRESH_WHITELIST_PREFIX}{user_id}:*")
            if keys:
                await r.delete(*keys)
            if cursor == 0:
                break
    except Exception as e:  # noqa: BLE001
        await redis_warn_once("auth.redis_down", str(e))


async def _consume_refresh_token(token: str, user_id: str) -> bool:
    """原子消费白名单（`SET key USED GET` 返回旧值，'1'→'USED' 状态机）。

    返回 False = 无 jti / 复用信号（旧值 USED，触发全量吊销）/ 用户被全量吊销 → 401；
    True = 正常消费（旧值 '1'）或未登记 fail-open（签发时 Redis 挂 / 键空间丢失）。
    Redis 故障 → fail-open 放行（轮换失效但可用，告警）。
    """
    try:
        jti = security.get_token_jti(token, security.TOKEN_TYPE_REFRESH)
    except Exception:  # noqa: BLE001  无 jti（轮换上线前签发的旧令牌）→ 拒绝
        return False
    try:
        r = get_redis()
        ttl = settings.jwt_refresh_token_expire_days * 86400
        # 全量吊销标记存在 → 该用户令牌已全部作废（键缺失 ≠ 未登记，必须 401）
        if await r.get(f"{_REFRESH_REVOKED_PREFIX}{user_id}") is not None:
            return False
        key = f"{_REFRESH_WHITELIST_PREFIX}{user_id}:{jti}"
        old = await r.set(key, _REFRESH_CONSUMED, get=True, ex=ttl)
        if old == _REFRESH_CONSUMED:
            # 复用信号：jti 已消费过（USED 标记）→ 泄露 → 全量吊销
            await _revoke_all_refresh(user_id)
            return False
        # 旧值 '1'（正常消费）或 None（未登记，fail-open 放行）；键均置 USED + TTL，
        # 该 jti 二次使用即命中复用检测
        return True
    except Exception as e:  # noqa: BLE001  Redis 故障 fail-open
        await redis_warn_once("auth.redis_down", str(e))
        return True


# ==================== 端点 ====================
@router.post("/login", response_model=TokenResponse, summary="登录")
async def login(
    req: LoginRequest,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> TokenResponse:
    logger.debug(
        "auth.login_request",
        username=req.username,
        password=redact(req.password),
    )
    ip = _client_ip(request)
    # 限流前置：冷却中 → 429（正确密码也拒绝）。S4 为 IP 级限流——惩罚攻击者
    # 自己的 IP，而非锁定被爆破账号（锁定账号反而是 DoS 向量）。
    if await _check_login_limited(ip):
        logger.info("auth.login_cooldown", ip=ip, username=req.username)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="登录尝试次数过多，请稍后再试",
        )
    # 每 IP 每窗口计数（含正确密码的尝试）：第 6 次 → 429 + 冷却（S4：5 次/min）
    if await _record_login_attempt(ip):
        logger.info("auth.login_rate_limited", ip=ip, username=req.username)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="登录尝试次数过多，请稍后再试",
        )
    user = await user_service.authenticate(session, req.username, req.password)
    if user is None:
        logger.info("auth.login_failed", ip=ip, username=req.username)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
        )
    await _clear_login_attempts(ip)

    refresh_token = security.create_refresh_token(user.user_id)
    await _register_refresh_jti(refresh_token, user.user_id)
    tokens = TokenResponse(
        access_token=security.create_access_token(user.user_id),
        refresh_token=refresh_token,
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
    # 自查 #6 轮换：GETDEL 原子消费白名单（S5）；旧 refresh 二次使用 →
    # _consume_refresh_token 内部触发该用户全量吊销 → 发新 access + 新 refresh
    if not await _consume_refresh_token(req.refresh_token, user_id):
        logger.info("auth.refresh_revoked", user_id=user_id)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="刷新令牌已失效，请重新登录",
        )
    new_refresh = security.create_refresh_token(user.user_id)
    await _register_refresh_jti(new_refresh, user.user_id)
    logger.info("auth.refresh_rotated", user_id=user_id)
    return AccessTokenResponse(
        access_token=security.create_access_token(user.user_id),
        refresh_token=new_refresh,
    )


@router.post("/change-password", response_model=dict, summary="修改密码")
async def change_password(
    req: ChangePasswordRequest,
    session: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_user_allow_change_password),
) -> dict:
    """修改当前用户密码（旧密码 + 新密码），改后清除首登强改标记。

    用 get_current_user_allow_change_password：未改密账号业务 API 被 403 拦截，
    改密端点本身必须豁免，否则永远改不了密码。
    """
    logger.debug("auth.change_password_request", user_id=user.user_id)
    try:
        await user_service.change_password(
            session, user, old_password=req.old_password, new_password=req.new_password
        )
    except user_service.InvalidOldPasswordError as e:
        logger.info("auth.change_password_bad_old", user_id=user.user_id)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except security.PasswordStrengthError as e:
        logger.info("auth.change_password_weak", user_id=user.user_id)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    logger.info("auth.password_changed", user_id=user.user_id)
    return {"message": "密码已修改"}

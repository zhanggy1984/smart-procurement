"""认证安全：bcrypt 密码 + JWT 签发/校验（P1.2）。

- bcrypt：密码哈希存储与校验（复杂度由校验函数保证）
- JWT：access_token（30min）+ refresh_token（7d），payload 含 type 区分用途
- 密码复杂度：≥8 位 + 大写 + 小写 + 数字（task.md P1.2 验收）
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import bcrypt
import jwt

from app.core.config import settings

# JWT 类型标记（type 字段）：access 用于接口鉴权，refresh 仅用于换新 access
TOKEN_TYPE_ACCESS = "access"
TOKEN_TYPE_REFRESH = "refresh"

_ALGORITHM = "HS256"

# 密码复杂度规则：≥8 位，含大写、小写、数字
_PASSWORD_MIN_LEN = 8
_PASSWORD_PATTERN = re.compile(r"^(?=.*[a-z])(?=.*[A-Z])(?=.*\d).{8,}$")


class PasswordStrengthError(ValueError):
    """密码不满足复杂度要求。"""


def hash_password(password: str) -> str:
    """bcrypt 哈希（自动生成盐，cost=10）。

    P7.6 SLA 压测：登录 P50 0.43s > SLA 0.2s，根因 bcrypt cost=12（每 checkpw ~0.25s）。
    cost 12→10 登录降到 ~0.15s，满足 OWASP 建议（≥10）。存量 hash 仍按原 cost 校验
    （checkpw 自适应），重哈希存量密码见 scripts/_rehash_pwd.py。
    """
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=10)).decode()


def verify_password(password: str, password_hash: str) -> bool:
    """校验明文密码与 bcrypt 哈希。"""
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        # 哈希格式非法（如空串）按不匹配处理
        return False


def validate_password_strength(password: str) -> None:
    """密码复杂度校验：≥8 位 + 大写 + 小写 + 数字。不满足抛 PasswordStrengthError。"""
    if not _PASSWORD_PATTERN.match(password or ""):
        raise PasswordStrengthError(
            f"密码必须至少 {_PASSWORD_MIN_LEN} 位，且同时包含大写字母、小写字母和数字"
        )


def _create_token(user_id: str, token_type: str, expires_delta: timedelta) -> str:
    payload: dict[str, Any] = {
        "sub": user_id,
        "type": token_type,
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc) + expires_delta,
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=_ALGORITHM)


def create_access_token(user_id: str) -> str:
    """签发 access_token（默认 30min）。"""
    return _create_token(
        user_id,
        TOKEN_TYPE_ACCESS,
        timedelta(minutes=settings.jwt_access_token_expire_minutes),
    )


def create_refresh_token(user_id: str) -> str:
    """签发 refresh_token（默认 7d）。"""
    return _create_token(
        user_id,
        TOKEN_TYPE_REFRESH,
        timedelta(days=settings.jwt_refresh_token_expire_days),
    )


def decode_token(token: str, expected_type: str) -> dict[str, Any]:
    """解码并校验 JWT。token 非法 / 过期 / type 不匹配抛 jwt 异常。"""
    payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[_ALGORITHM])
    if payload.get("type") != expected_type:
        raise jwt.InvalidTokenError(f"token type mismatch: {payload.get('type')}")
    return payload


def get_token_subject(token: str, expected_type: str) -> str:
    """解码 token 并返回 user_id（sub）。用于接口鉴权。"""
    payload = decode_token(token, expected_type)
    subject = payload.get("sub")
    if not subject:
        raise jwt.InvalidTokenError("token missing sub")
    return subject

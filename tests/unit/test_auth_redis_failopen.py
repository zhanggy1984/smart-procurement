"""自查 #6 异常兜底：认证 Redis 调用降级（登录限流 fail-open / refresh 轮换 fail-open）。

对齐 reviews 的 fail-open 覆盖（test_reviews_redis.py）：Redis 不可用时登录不被限流
阻断、refresh 白名单校验跳过（退回纯 JWT 可用）；Redis 恢复后「签发时未登记」的
token 不误判为复用、不触发全量吊销；而真正的复用（USED 标记）仍被识别并吊销。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.v1 import auth
from app.core import security


def _refresh_token(user_id: str) -> str:
    """签发真实 refresh token（含 jti，供 helper 解析）。"""
    return security.create_refresh_token(user_id)


class _RedisDown:
    """模拟 Redis 不可用：所有调用抛异常（auth 的 fail-open 目标）。"""

    async def get(self, *a, **kw):
        raise ConnectionError("redis conn refused")

    async def set(self, *a, **kw):
        raise ConnectionError("redis conn refused")

    async def delete(self, *a, **kw):
        raise ConnectionError("redis conn refused")

    async def scan(self, *a, **kw):
        raise ConnectionError("redis conn refused")


def _patch_redis(monkeypatch, fake_r):
    monkeypatch.setattr(auth, "get_redis", lambda: fake_r)


# ==================== 登录限流 fail-open ====================


@pytest.mark.asyncio
async def test_login_helpers_fail_open_when_redis_down(monkeypatch):
    """Redis 挂 → 登录限流三件套全部 fail-open：不冷却、不计数拦截、不抛。"""
    _patch_redis(monkeypatch, _RedisDown())
    assert await auth._check_login_limited("1.2.3.4") is False
    assert await auth._record_login_attempt("1.2.3.4") is False  # 不拦截登录
    await auth._clear_login_attempts("1.2.3.4")  # 不抛


# ==================== refresh 轮换 fail-open ====================


@pytest.mark.asyncio
async def test_consume_refresh_fail_open_when_redis_down(monkeypatch):
    """Redis 挂 → _consume_refresh_token 放行（退回纯 JWT 可用，不抛 401）。"""
    _patch_redis(monkeypatch, _RedisDown())
    assert await auth._consume_refresh_token(_refresh_token("U-1"), "U-1") is True


@pytest.mark.asyncio
async def test_consume_unregistered_token_accepted_after_recovery(monkeypatch):
    """签发时 Redis 挂（未登记），恢复后 refresh → fail-open 放行，不误判复用、不吊销。

    这是 GETDEL-None 歧义的修复点：键缺失 ≠ 复用。白名单键缺失 + 无全量吊销标记
    → 按「未登记」放行；绝不能触发 _revoke_all_refresh。
    """
    r = MagicMock()
    r.get = AsyncMock(return_value=None)  # refresh:revoke:{uid} 不存在
    r.set = AsyncMock(return_value=None)  # 白名单键缺失 → SET GET 返回 None（未登记）
    _patch_redis(monkeypatch, r)
    assert await auth._consume_refresh_token(_refresh_token("U-1"), "U-1") is True
    # 未触发全量吊销：不应写入 refresh:revoke:{uid} 标记
    assert not any("refresh:revoke:" in str(c) for c in r.set.call_args_list)


@pytest.mark.asyncio
async def test_consume_refresh_reuse_revokes_all(monkeypatch):
    """复用信号：旧值 USED → 触发全量吊销（写 refresh:revoke:{uid}）→ 拒绝 401。"""
    r = MagicMock()
    r.get = AsyncMock(return_value=None)  # 无全量吊销标记
    r.set = AsyncMock(return_value=auth._REFRESH_CONSUMED)  # 旧值 USED（已消费过）
    r.scan = AsyncMock(return_value=(0, []))
    r.delete = AsyncMock(return_value=0)
    _patch_redis(monkeypatch, r)
    assert await auth._consume_refresh_token(_refresh_token("U-1"), "U-1") is False
    assert any("refresh:revoke:U-1" in str(c) for c in r.set.call_args_list)


@pytest.mark.asyncio
async def test_consume_refresh_revoked_user_rejected(monkeypatch):
    """用户已被全量吊销 → 即使白名单键缺失也 401（吊销不因 fail-open 失效）。"""
    r = MagicMock()
    r.get = AsyncMock(return_value="1")  # refresh:revoke:{uid} 存在
    r.set = AsyncMock()
    _patch_redis(monkeypatch, r)
    assert await auth._consume_refresh_token(_refresh_token("U-1"), "U-1") is False
    r.set.assert_not_awaited()  # 吊销判定优先，不触碰白名单


@pytest.mark.asyncio
async def test_consume_refresh_valid_consumes(monkeypatch):
    """正常消费：旧值 '1'（在册未消费）→ 放行 + 置 USED。"""
    r = MagicMock()
    r.get = AsyncMock(return_value=None)
    r.set = AsyncMock(return_value="1")
    _patch_redis(monkeypatch, r)
    assert await auth._consume_refresh_token(_refresh_token("U-1"), "U-1") is True

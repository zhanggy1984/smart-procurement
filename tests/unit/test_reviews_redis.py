"""P8 异常兜底：Redis 业务调用降级（幂等 fail-open / SSE 缓存跳过）。

覆盖 reviews.py 三个 Redis helper：Redis 不可用时幂等放行、SSE 帧缓存跳过
（不阻断流）、缓存读取返回 None（断流重连走 event:reset 全量重拉）。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.api.v1 import reviews


class _RedisDown:
    """模拟 Redis 不可用：所有调用抛异常（reviews 的 fail-open 目标）。"""

    async def set(self, *a, **kw):
        raise ConnectionError("redis conn refused")

    async def rpush(self, *a, **kw):
        raise ConnectionError("redis conn refused")

    async def expire(self, *a, **kw):
        raise ConnectionError("redis conn refused")

    async def lrange(self, *a, **kw):
        raise ConnectionError("redis conn refused")


@pytest.mark.asyncio
async def test_idempotency_fail_open_when_redis_down(monkeypatch):
    """Redis 挂 → _check_idempotency 不抛（fail-open 放行，评分主链路不中断）。"""
    monkeypatch.setattr(reviews, "_idem_redis", AsyncMock(return_value=_RedisDown()))
    await reviews._check_idempotency("key-1", "R1")  # 不抛即放行


@pytest.mark.asyncio
async def test_cache_sse_frame_skips_when_redis_down(monkeypatch):
    """Redis 挂 → _cache_sse_frame 跳过缓存（不阻断流）。"""
    monkeypatch.setattr(reviews, "_idem_redis", AsyncMock(return_value=_RedisDown()))
    await reviews._cache_sse_frame("R1", "id: 1\nevent: x\ndata: {}\n\n")  # 不抛


@pytest.mark.asyncio
async def test_load_sse_cache_returns_none_when_redis_down(monkeypatch):
    """Redis 挂 → _load_sse_cache 返回 None（reconnect 走 event:reset 全量重拉）。"""
    monkeypatch.setattr(reviews, "_idem_redis", AsyncMock(return_value=_RedisDown()))
    assert await reviews._load_sse_cache("R1") is None


@pytest.mark.asyncio
async def test_idempotency_duplicate_still_422(monkeypatch):
    """Redis 正常但 key 重复 → 仍 422（幂等语义不因降级丢失）。"""
    r = MagicMock()
    r.set = AsyncMock(return_value=False)  # nx 未占用 → 重复请求
    monkeypatch.setattr(reviews, "_idem_redis", AsyncMock(return_value=r))
    with pytest.raises(HTTPException) as ei:
        await reviews._check_idempotency("key-dup", "R1")
    assert ei.value.status_code == 422


@pytest.mark.asyncio
async def test_idempotency_ok_sets_key(monkeypatch):
    """Redis 正常 + 新 key → 设置成功不抛。"""
    r = MagicMock()
    r.set = AsyncMock(return_value=True)
    monkeypatch.setattr(reviews, "_idem_redis", AsyncMock(return_value=r))
    await reviews._check_idempotency("key-new", "R1")
    r.set.assert_awaited_once()

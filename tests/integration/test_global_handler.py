"""P8 全局 exception handler 集成测试。

- 未捕获非依赖异常 → 500 + JSON 响应体（{"detail": "服务器内部错误，请稍后重试"}，
  替代 FastAPI 默认纯文本，供前端统一解析）
- 依赖异常（Redis 连接错误）→ 503 JSON（{"detail": "核心依赖暂不可用，请稍后重试"}，
  前端走降级 UI）
"""

from __future__ import annotations

import pytest

from app.main import app


@pytest.mark.asyncio
async def test_unhandled_exception_returns_500_json(client):
    """未捕获 RuntimeError → 500 + JSON 响应体。"""
    @app.get("/_test/boom-500")
    async def _boom():
        raise RuntimeError("内部服务器 bug")

    resp = await client.get("/_test/boom-500")
    assert resp.status_code == 500
    assert resp.json()["detail"] == "服务器内部错误，请稍后重试"


@pytest.mark.asyncio
async def test_dependency_error_returns_503(client):
    """Redis 连接错误 → 503（前端走降级 UI 而非 500）。"""
    from redis.exceptions import ConnectionError as RedisConnectionError

    @app.get("/_test/redis-down-503")
    async def _redis_down():
        raise RedisConnectionError("redis conn refused")

    resp = await client.get("/_test/redis-down-503")
    assert resp.status_code == 503
    assert resp.json()["detail"] == "核心依赖暂不可用，请稍后重试"

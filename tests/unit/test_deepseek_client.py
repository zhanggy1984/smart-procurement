"""P7.2 DeepSeek Client 容错单元测试（task.md：6 用例）。

覆盖 _retry_schedule / _is_fuse_failure / 断路器状态机：
429 重试退避、5xx 退避、401/403 不重试、超时/5xx 计入熔断、429 不熔断、
半开探测成功 CLOSE / 失败重回 OPEN。
"""

from __future__ import annotations

import asyncio

import openai
import pytest

from app.ai.llm.deepseek_client import (
    _CircuitBreaker,
    _is_fuse_failure,
    _retry_schedule,
    CircuitOpenError,
)


def test_retry_schedule_by_status():
    """重试退避按状态码：429→(1,2,4)，5xx→(0.5,1,3)，401/403→()。"""
    assert _retry_schedule(429) == (1, 2, 4)
    assert _retry_schedule(502) == (0.5, 1.0, 3.0)
    assert _retry_schedule(503) == (0.5, 1.0, 3.0)
    assert _retry_schedule(504) == (0.5, 1.0, 3.0)
    assert _retry_schedule(401) == ()
    assert _retry_schedule(403) == ()
    assert _retry_schedule(None) == ()  # 无状态码不重试


def _mk_openai_error(status_code: int) -> Exception:
    """构造带 status_code 的 openai 异常（重试/熔断判定用 status_code）。"""
    if status_code in (400, 401, 403):
        return openai.AuthenticationError(
            "auth failed", response=__import__("httpx").Response(status_code, request=__import__("httpx").Request("POST", "http://x")), body=None
        )
    if status_code == 429:
        return openai.RateLimitError(
            "rate limited", response=__import__("httpx").Response(429, request=__import__("httpx").Request("POST", "http://x")), body=None
        )
    return openai.InternalServerError(
        "server error", response=__import__("httpx").Response(status_code, request=__import__("httpx").Request("POST", "http://x")), body=None
    )


def test_is_fuse_failure_matrix():
    """熔断失败判定：超时/连接/5xx 计入，429 限流与 401/403 配置错误不计入。"""
    assert _is_fuse_failure(_mk_openai_error(500)) is True
    assert _is_fuse_failure(_mk_openai_error(503)) is True
    assert _is_fuse_failure(_mk_openai_error(429)) is False
    assert _is_fuse_failure(_mk_openai_error(401)) is False
    assert _is_fuse_failure(_mk_openai_error(403)) is False
    assert _is_fuse_failure(openai.APITimeoutError(request=__import__("httpx").Request("POST", "http://x"))) is True
    assert _is_fuse_failure(openai.APIConnectionError(request=__import__("httpx").Request("POST", "http://x"))) is True
    assert _is_fuse_failure(ValueError("unknown")) is False


@pytest.mark.asyncio
async def test_circuit_opens_after_threshold():
    """连续失败达阈值 → OPEN，期间 acquire 抛 CircuitOpenError。"""
    cb = _CircuitBreaker(threshold=3, open_seconds=30)
    for _ in range(3):
        await cb.record_failure()
    assert cb.state == "OPEN"
    with pytest.raises(CircuitOpenError):
        await cb.acquire()


@pytest.mark.asyncio
async def test_circuit_half_open_probe_success():
    """到期后 acquire 转 HALF_OPEN 并放行（串行路径），成功 → CLOSED 并清零计数。

    注意：并发下半开期间所有请求均放行，并非"仅 1 次探测"（定案暂不收紧，
    见 deepseek_client 模块 docstring）。
    """
    cb = _CircuitBreaker(threshold=2, open_seconds=0.05)
    await cb.record_failure()
    await cb.record_failure()
    assert cb.state == "OPEN"
    await asyncio.sleep(0.06)  # 等待熔断到期
    await cb.acquire()  # 到期 → HALF_OPEN
    assert cb.state == "HALF_OPEN"
    await cb.record_success()
    assert cb.state == "CLOSED"


@pytest.mark.asyncio
async def test_circuit_half_open_probe_fail_reopens():
    """半开探测失败 → 重回 OPEN 且重新计时（串行路径；并发竞态见 deepseek_client 模块 docstring）。"""
    cb = _CircuitBreaker(threshold=2, open_seconds=0.05)
    await cb.record_failure()
    await cb.record_failure()
    await asyncio.sleep(0.06)
    await cb.acquire()
    assert cb.state == "HALF_OPEN"
    await cb.record_failure()
    assert cb.state == "OPEN"

"""DeepSeek Client + 容错（P3.1）。

- chat_stream()：SSE 流式调用（temperature=0.3），async generator 逐段吐文本
- chat()：非流式便捷封装（摘要/结构化调用）
- 断路器：连续 N 次超时/5xx → OPEN（熔断 30s）→ 到期后首个 acquire 转 HALF_OPEN 放行，
  半开期间后续 acquire 一并放行（无单探测并发门——定案暂不收紧，见 memory
  circuit-breaker-half-open-concurrency）。探测成功 CLOSE / 失败重回 OPEN。
  N 由 DEEPSEEK_CIRCUIT_BREAKER_THRESHOLD（默认 5）。
- 重试（task.md P3.1）：
  - 429（限流）→ 1s/2s/4s 退避，重试 3 次，不熔断
  - 502/503 → 0.5s/1s/3s，重试 2 次（比 429 少 1 次），且计入熔断失败
  - 401/403 → 不重试（配置错误）
  - 超时/连接错误 → 计入熔断失败，按 502 退避重试
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Optional

import openai
import structlog

from app.core.config import settings
from app.services import config_service

logger = structlog.get_logger(__name__)

# 熔断状态
CIRCUIT_CLOSED = "CLOSED"
CIRCUIT_OPEN = "OPEN"
CIRCUIT_HALF_OPEN = "HALF_OPEN"

# 429 与 502/503 的重试退避（秒），index 依次取，耗尽抛错
_BACKOFF_429 = (1, 2, 4)
_BACKOFF_5XX = (0.5, 1.0, 3.0)
# 401/403 不重试
_NO_RETRY = ()

# 熔断半开探测窗口
CIRCUIT_OPEN_SECONDS = 30.0


class CircuitOpenError(RuntimeError):
    """断路器 OPEN：AI 服务暂不可用，调用方降级（纯人工评审）。"""


class _CircuitBreaker:
    """断路器状态机。asyncio.Lock 保证多协程并发安全。"""

    def __init__(self, threshold: int, open_seconds: float = CIRCUIT_OPEN_SECONDS) -> None:
        self._threshold = threshold
        self._open_seconds = open_seconds
        self._state = CIRCUIT_CLOSED
        self._failure_count = 0
        self._open_until = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """调用前检查。OPEN 未到期 → 抛 CircuitOpenError；到期 → 转 HALF_OPEN 放行。

        注意：HALF_OPEN 期间不拦截（无单探测并发门），OPEN 到期瞬间的并发请求会
        全部放行。当前场景（云 API + 低并发）影响中低，定案暂不收紧——见模块 docstring。
        """
        async with self._lock:
            if self._state == CIRCUIT_OPEN and time.monotonic() < self._open_until:
                raise CircuitOpenError("AI 服务熔断中（断路器 OPEN），请稍后重试")
            if self._state == CIRCUIT_OPEN and time.monotonic() >= self._open_until:
                self._state = CIRCUIT_HALF_OPEN
                logger.info("circuit.half_open")

    async def record_failure(self) -> None:
        """失败计数；达到阈值 → OPEN 熔断。HALF_OPEN 探测失败 → 重回 OPEN。"""
        async with self._lock:
            self._failure_count += 1
            if self._failure_count >= self._threshold:
                self._state = CIRCUIT_OPEN
                self._open_until = time.monotonic() + self._open_seconds
                self._failure_count = 0
                logger.warning("circuit.open", threshold=self._threshold, open_seconds=self._open_seconds)
            elif self._state == CIRCUIT_HALF_OPEN:
                self._state = CIRCUIT_OPEN
                self._open_until = time.monotonic() + self._open_seconds
                logger.warning("circuit.open_after_probe")

    async def record_success(self) -> None:
        """成功 → CLOSED 并重置计数。"""
        async with self._lock:
            self._state = CIRCUIT_CLOSED
            self._failure_count = 0

    @property
    def state(self) -> str:
        return self._state


def _retry_schedule(status_code: int) -> tuple[float, ...]:
    """按状态码返回重试退避序列。429→(1,2,4)，5xx→(0.5,1,3)，401/403→()。"""
    if status_code == 429:
        return _BACKOFF_429
    if status_code in (502, 503, 504):
        return _BACKOFF_5XX
    return _NO_RETRY


def _is_fuse_failure(exc: Exception) -> bool:
    """是否计入熔断失败：超时、连接错误、5xx（429 只限流不熔断，401/403 配置错误不熔断）。"""
    if isinstance(exc, (openai.APITimeoutError, openai.APIConnectionError)):
        return True
    if isinstance(exc, openai.InternalServerError):
        return True
    return False


class DeepSeekClient:
    """DeepSeek 对话客户端（断路器 + 退避重试）。进程内单例（get_client()）。"""

    def __init__(self) -> None:
        self._circuit = _CircuitBreaker(settings.deepseek_circuit_breaker_threshold)
        self._client = openai.AsyncOpenAI(
            base_url=settings.deepseek_base_url,
            api_key=settings.deepseek_api_key,
            timeout=settings.deepseek_timeout,
        )

    async def chat_stream(
        self,
        messages: list[dict],
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> AsyncIterator[tuple[str, dict | None]]:
        """SSE 流式调用，逐段 yield (文本增量, usage_or_None)。熔断 OPEN 抛 CircuitOpenError。

        stream_options.include_usage=True 时，最后一个 chunk 携带 usage（choices 为空），
        yield ("", usage_dict)；调用方按流聚合后透出契约 usage 事件。
        temperature/max_tokens 缺省时读系统配置（P6.2，llm.temperature/max_tokens），
        显式传参覆盖配置。重试仅发生在真正发送请求前失败/响应阶段（流开始前）；
        已开始流式输出后中断不再重试（避免重复输出），直接抛异常由调用方/SSE 端处理。
        """
        if temperature is None:
            temperature = float(config_service.get_sync("llm.temperature"))
        if max_tokens is None:
            max_tokens = int(config_service.get_sync("llm.max_tokens"))
        await self._circuit.acquire()
        if not settings.deepseek_enabled:
            raise CircuitOpenError("AI 服务已停用（DEEPSEEK_ENABLED=false），请人工评审")
        attempts = 0
        while True:
            try:
                stream = await self._client.chat.completions.create(
                    model=settings.deepseek_model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    stream=True,
                    stream_options={"include_usage": True},
                )
                async for chunk in stream:
                    if chunk.usage:
                        # include_usage 的最后一个 chunk：usage 非空、choices 为空
                        yield "", chunk.usage.model_dump()
                        continue
                    if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                        yield chunk.choices[0].delta.content, None
                await self._circuit.record_success()
                return
            except CircuitOpenError:
                raise
            except Exception as e:  # noqa: BLE001  openai 各类异常统一按状态码处理
                status = getattr(e, "status_code", None)
                schedule = _retry_schedule(status) if status else _BACKOFF_5XX
                if _is_fuse_failure(e):
                    await self._circuit.record_failure()
                if isinstance(e, (openai.AuthenticationError, openai.PermissionDeniedError)):
                    logger.error("llm.auth_failed", error=str(e))
                    raise
                if attempts >= len(schedule):
                    raise
                delay = schedule[attempts]
                attempts += 1
                logger.warning("llm.retry", status=status, attempt=attempts, delay=delay, error=str(e))
                await asyncio.sleep(delay)

    async def chat_stream_agent(
        self,
        messages: list[dict],
        tools: list[dict],
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> AsyncIterator[dict]:
        """SSE 流式调用，支持 DeepSeek function calling（agent 决策轮）。

        供 agent 编排消费（决策轮事件不作 SSE 三发契约输出，由上层决定取舍），yield 事件：
        - {"type": "content", "delta": str}：正文增量（含 <thinking>/<answer> 标签原文，
          由上层 ThinkingAnswerSplitter 切分）
        - {"type": "tool_call", "tool_calls": [...]}：finish_reason=="tool_calls" 时一次性
          flush 完整 tool_calls（arguments 为 JSON 字符串，调用方 json.loads 取参数）
        - {"type": "usage", "usage": dict}：流末尾 include_usage 的 usage chunk

        重试/断路器语义与 chat_stream 完全一致（429/5xx 退避、OPEN 熔断、流开始前重试）。
        """
        if temperature is None:
            temperature = float(config_service.get_sync("llm.temperature"))
        if max_tokens is None:
            max_tokens = int(config_service.get_sync("llm.max_tokens"))
        await self._circuit.acquire()
        if not settings.deepseek_enabled:
            raise CircuitOpenError("AI 服务已停用（DEEPSEEK_ENABLED=false），请人工评审")
        attempts = 0
        while True:
            try:
                stream = await self._client.chat.completions.create(
                    model=settings.deepseek_model,
                    messages=messages,
                    tools=tools,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    stream=True,
                    stream_options={"include_usage": True},
                )
                # tool_calls 按 index 累加（id/name 首个分片携带，arguments 增量拼接）
                tool_acc: dict[int, dict] = {}
                async for chunk in stream:
                    if chunk.usage:
                        yield {"type": "usage", "usage": chunk.usage.model_dump()}
                        continue
                    if not chunk.choices:
                        continue
                    choice = chunk.choices[0]
                    delta = choice.delta
                    if delta and delta.tool_calls:
                        for tc in delta.tool_calls:
                            entry = tool_acc.setdefault(
                                tc.index, {"id": None, "name": "", "arguments": ""}
                            )
                            if tc.id:
                                entry["id"] = tc.id
                            fn = getattr(tc, "function", None)
                            if fn:
                                if fn.name:
                                    entry["name"] = fn.name
                                if fn.arguments:
                                    entry["arguments"] += fn.arguments
                    if delta and delta.content:
                        yield {"type": "content", "delta": delta.content}
                    # 结束信号：finish_reason=="tool_calls" 的 chunk 通常带着最后一个 tool_calls
                    if choice.finish_reason == "tool_calls" and tool_acc:
                        yield {
                            "type": "tool_call",
                            "tool_calls": [
                                {"id": e["id"], "type": "function",
                                 "function": {"name": e["name"], "arguments": e["arguments"]}}
                                for e in tool_acc.values()
                            ],
                        }
                        tool_acc.clear()
                await self._circuit.record_success()
                return
            except CircuitOpenError:
                raise
            except Exception as e:  # noqa: BLE001  openai 各类异常统一按状态码处理
                status = getattr(e, "status_code", None)
                schedule = _retry_schedule(status) if status else _BACKOFF_5XX
                if _is_fuse_failure(e):
                    await self._circuit.record_failure()
                if isinstance(e, (openai.AuthenticationError, openai.PermissionDeniedError)):
                    logger.error("llm.auth_failed", error=str(e))
                    raise
                if attempts >= len(schedule):
                    raise
                delay = schedule[attempts]
                attempts += 1
                logger.warning("llm.retry", status=status, attempt=attempts, delay=delay, error=str(e))
                await asyncio.sleep(delay)

    async def chat(
        self,
        messages: list[dict],
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        """非流式对话（摘要/结构化输出等）。返回完整文本。

        参数缺省时读系统配置（同 chat_stream），显式传参覆盖。
        """
        if temperature is None:
            temperature = float(config_service.get_sync("llm.temperature"))
        if max_tokens is None:
            max_tokens = int(config_service.get_sync("llm.max_tokens"))
        await self._circuit.acquire()
        if not settings.deepseek_enabled:
            raise CircuitOpenError("AI 服务已停用（DEEPSEEK_ENABLED=false），请人工评审")
        attempts = 0
        while True:
            try:
                resp = await self._client.chat.completions.create(
                    model=settings.deepseek_model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                await self._circuit.record_success()
                return resp.choices[0].message.content or ""
            except CircuitOpenError:
                raise
            except Exception as e:  # noqa: BLE001
                status = getattr(e, "status_code", None)
                schedule = _retry_schedule(status) if status else _BACKOFF_5XX
                if _is_fuse_failure(e):
                    await self._circuit.record_failure()
                if isinstance(e, (openai.AuthenticationError, openai.PermissionDeniedError)):
                    logger.error("llm.auth_failed", error=str(e))
                    raise
                if attempts >= len(schedule):
                    raise
                delay = schedule[attempts]
                attempts += 1
                logger.warning("llm.retry", status=status, attempt=attempts, delay=delay, error=str(e))
                await asyncio.sleep(delay)

    @property
    def circuit_state(self) -> str:
        return self._circuit.state


_client: DeepSeekClient | None = None


def get_client() -> DeepSeekClient:
    """模块级单例。"""
    global _client
    if _client is None:
        _client = DeepSeekClient()
    return _client

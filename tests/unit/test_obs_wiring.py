"""§11.3 smart-procurement 观测接入接线单测（tests/unit，offline 不连外部服务）。

覆盖面（app.obs 单一 seam → monkeypatch app.obs.obs 或各消费方已绑定的 helper 别名）：
- obs() 门：三要素缺一 → None；齐备但包缺失 → None（边带不阻塞业务）；齐备+包在 → 返回 sdk
- helper（begin/end/record_llm_ok/record_llm_error/init/shutdown）在门开时正确转发到 sdk、
  门关/未初始化时零动作、sdk 自身抛异常一律吞掉（观测边带不炸业务）
- llm_error_type 分类：仅 429 单列 llm_rate_limit，否则类名关键词（timeout/connect/rate），
  其余（含 auth 类）统一 llm_other —— 全部落平台白名单值域
- middleware：豁免路径不建 span；非豁免 ok/HTTP_{code}/CLIENT_DISCONNECT（body 迭代期收口、
  断连走 aborted）；call_next 抛异常 → UNHANDLED_EXCEPTION
- deepseek_client 三方法 + conversation_service._summarize_with_llm 旁路：
  成功 ok-with-usage、失败先记 error 再抛/兜底（§2.4）、熔断/停用前置拒绝不打点

注：obs_sdk 不经 poetry.lock（Docker additional_contexts 注入），本仓测试环境无包；
    所有 sdk 交互经 app.obs.obs 门 / 消费方 helper 别名 monkeypatch 替代，不依赖真实包
    （sdk 自身 structlog processor 正确性由 sdk/tests/test_structlog.py 覆盖）。
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.requests import Request
from starlette.responses import PlainTextResponse, StreamingResponse

from app.core.config import settings
from app.services.conversation_service import _summarize_with_llm
from app import obs as obs_mod
from app.ai.llm import deepseek_client as dc
from app.core import middleware as mw


# ==================== 观测 fake ====================


class _FakeSdk:
    """记录调用的 obs_sdk 替身（app.obs.obs 的返回物）。"""

    def __init__(self, raise_on: bool = False):
        self.calls: list[tuple] = []
        self._raise_on = raise_on

    def _guard(self):
        if self._raise_on:
            raise RuntimeError("sdk boom")

    def init(self, agent, **kw):
        self._guard()
        self.calls.append(("init", agent, kw))

    def shutdown(self):
        self._guard()
        self.calls.append(("shutdown",))

    def begin_request(self, **kw):
        self._guard()
        self.calls.append(("begin_request", kw))

    def end_request(self, status, **kw):
        self._guard()
        self.calls.append(("end_request", status, kw))

    def record_llm(self, model, status, **kw):
        self._guard()
        self.calls.append(("record_llm", model, status, kw))


class _Usage:
    """openai SDK usage chunk 是 pydantic 对象（生产代码调 .model_dump()）。"""

    def __init__(self, **kw):
        self._d = kw

    def model_dump(self):
        return dict(self._d)


def _open_gate(monkeypatch, fake=None):
    """打开 obs() 门：三要素齐备 + obs() 返回 fake（默认记录型 _FakeSdk）。"""
    monkeypatch.setattr(settings, "obs_enabled", True)
    monkeypatch.setattr(settings, "obs_kafka_servers", "kafka:9092")
    monkeypatch.setattr(settings, "obs_kafka_topic", "dev.obs.agent.smart-procurement")
    fake = fake or _FakeSdk()
    monkeypatch.setattr(obs_mod, "obs", lambda: fake)
    return fake


def _close_gate(monkeypatch):
    """关闭 obs() 门：三要素缺一（settings 默认即关，防 env 干扰显式归零）。"""
    monkeypatch.setattr(settings, "obs_enabled", False)
    monkeypatch.setattr(settings, "obs_kafka_servers", "")
    monkeypatch.setattr(settings, "obs_kafka_topic", "")


# ==================== obs() 门 ====================


def test_obs_gate_disabled_returns_none(monkeypatch):
    """三要素缺一 → obs() 为 None（业务零侵入）。"""
    _close_gate(monkeypatch)
    assert obs_mod.obs() is None


def test_obs_gate_enabled_but_missing_package(monkeypatch):
    """齐备但 obs_sdk 未安装 → None（ImportError 吞掉，不打炸业务）。

    注意：不能 _open_gate()——那会把 obs() 本身替换掉；本测试要跑 obs() 真实代码
    走 `import obs_sdk`，故只开三要素、不 patch obs()。
    """
    monkeypatch.setattr(settings, "obs_enabled", True)
    monkeypatch.setattr(settings, "obs_kafka_servers", "kafka:9092")
    monkeypatch.setattr(settings, "obs_kafka_topic", "dev.obs.agent.smart-procurement")
    # 测试环境无 obs_sdk；obs() 内 import 失败应走 except → None
    monkeypatch.delitem(sys.modules, "obs_sdk", raising=False)
    assert obs_mod.obs() is None


def test_obs_gate_enabled_returns_sdk(monkeypatch):
    """齐备 + sys.modules 注入 fake 包 → obs() 返回该模块（import 命中 sys.modules）。"""
    fake_pkg = SimpleNamespace(marker="pkg")
    monkeypatch.setitem(sys.modules, "obs_sdk", fake_pkg)
    monkeypatch.setattr(settings, "obs_enabled", True)
    monkeypatch.setattr(settings, "obs_kafka_servers", "kafka:9092")
    monkeypatch.setattr(settings, "obs_kafka_topic", "dev.obs.agent.smart-procurement")
    assert obs_mod.obs() is fake_pkg


# ==================== helper 路由 / 吞错 ====================


def test_begin_request_forwards_when_open(monkeypatch):
    """门开 → begin_request 把 method/path/trace_id 原样传给 sdk，返回 True。"""
    fake = _open_gate(monkeypatch)
    assert obs_mod.begin_request(method="POST", path="/api/v1/reviews", trace_id="rid1") is True
    assert fake.calls == [("begin_request", {"method": "POST", "path": "/api/v1/reviews",
                                             "trace_id": "rid1"})]


def test_begin_request_noop_when_closed(monkeypatch):
    """门关 → begin_request 返回 False（不碰 sdk）。"""
    _close_gate(monkeypatch)
    assert obs_mod.begin_request(method="GET", path="/x") is False


def test_end_request_ok_when_open(monkeypatch):
    """门开 → end_request ok 透传；error_type/error_msg 随 error 透传。

    app.obs.end_request 恒把 error_type/error_msg 两 kwarg 传给 sdk（ok 时二者为 None）。
    """
    fake = _open_gate(monkeypatch)
    obs_mod.end_request("ok")
    obs_mod.end_request("error", error_type="HTTP_503", error_msg="上游熔断")
    assert fake.calls == [("end_request", "ok", {"error_type": None, "error_msg": None}),
                          ("end_request", "error", {"error_type": "HTTP_503", "error_msg": "上游熔断"})]


def test_end_request_noop_when_closed(monkeypatch):
    """门关 → end_request 零动作。"""
    _close_gate(monkeypatch)
    obs_mod.end_request("error", error_type="HTTP_500", error_msg="x")  # 不应抛


def test_record_llm_ok_usage_forwarded(monkeypatch):
    """record_llm_ok：status=ok + model=settings.deepseek_model + duration_ms ≥0 + usage 透传。"""
    fake = _open_gate(monkeypatch)
    started = obs_mod.llm_start()
    obs_mod.record_llm_ok(started, usage={"total_tokens": 15})
    assert len(fake.calls) == 1
    name, model, status, kw = fake.calls[0]
    assert (name, model, status) == ("record_llm", settings.deepseek_model, "ok")
    assert isinstance(kw["duration_ms"], int) and kw["duration_ms"] >= 0
    assert kw["usage"] == {"total_tokens": 15}


def test_record_llm_error_type_and_truncate(monkeypatch):
    """record_llm_error：显式 error_type 优先生效；msg 截 512。"""
    fake = _open_gate(monkeypatch)
    started = obs_mod.llm_start()
    obs_mod.record_llm_error(started, ValueError("x" * 1000), error_type="HTTP_429")
    assert len(fake.calls) == 1
    _, model, status, kw = fake.calls[0]
    assert (model, status) == (settings.deepseek_model, "error")
    assert kw["error_type"] == "HTTP_429"
    assert len(kw["error_msg"]) == 512


def test_record_llm_error_default_classify(monkeypatch):
    """record_llm_error 未给 error_type → 走 llm_error_type 分类。"""
    fake = _open_gate(monkeypatch)
    started = obs_mod.llm_start()
    obs_mod.record_llm_error(started, ValueError("boom"))
    assert fake.calls[0][3]["error_type"] == "llm_other"


def test_helpers_swallow_sdk_exceptions(monkeypatch):
    """sdk 自身抛异常 → helper 全部吞掉，业务不炸（begin_request 返回 False）。"""
    _open_gate(monkeypatch, fake=_FakeSdk(raise_on=True))
    assert obs_mod.begin_request(method="GET", path="/x") is False
    obs_mod.end_request("ok")
    obs_mod.record_llm_ok(obs_mod.llm_start())
    obs_mod.record_llm_error(obs_mod.llm_start(), ValueError("boom"))


def test_init_shutdown_forward_when_open(monkeypatch):
    """init_obs/shutdown_obs：门开 → init 用 structlog mode 装配，shutdown 收尾。"""
    fake = _open_gate(monkeypatch)
    obs_mod.init_obs()
    obs_mod.shutdown_obs()
    init_calls = [c for c in fake.calls if c[0] == "init"]
    assert len(init_calls) == 1
    _, agent, kw = init_calls[0]
    assert agent == "smart-procurement"
    assert kw["log_mode"] == "structlog"
    assert kw["kafka_servers"] == "kafka:9092"
    assert fake.calls[-1][0] == "shutdown"


def test_init_shutdown_noop_when_closed(monkeypatch):
    """门关 → init/shutdown 零动作（lifespan 未启用场景安全）。"""
    _close_gate(monkeypatch)
    obs_mod.init_obs()
    obs_mod.shutdown_obs()


# ==================== llm_error_type 分类 ====================


class _StatusErr(Exception):
    pass


class _MyTimeoutError(Exception):
    pass


class _MyConnectionError(Exception):
    pass


class _MyRateLimitError(Exception):
    pass


class _MyAuthenticationError(Exception):
    pass


class _MyPermissionDeniedError(Exception):
    pass


def test_llm_error_type_http_code_first():
    """带 status_code 的异常 → 仅 429 单列 llm_rate_limit，其余码位统一 llm_other。"""
    e = _StatusErr("x")
    e.status_code = 429
    assert obs_mod.llm_error_type(e) == "llm_rate_limit"
    e.status_code = 401
    assert obs_mod.llm_error_type(e) == "llm_other"
    e.status_code = 503
    assert obs_mod.llm_error_type(e) == "llm_other"


def test_llm_error_type_class_name_keywords():
    """无 status_code → 按类名关键词归并（auth 类白名单无对应词，归 llm_other）。"""
    assert obs_mod.llm_error_type(_MyTimeoutError("t")) == "llm_timeout"
    assert obs_mod.llm_error_type(_MyConnectionError("c")) == "llm_connection"
    assert obs_mod.llm_error_type(_MyRateLimitError("r")) == "llm_rate_limit"
    assert obs_mod.llm_error_type(_MyAuthenticationError("a")) == "llm_other"
    assert obs_mod.llm_error_type(_MyPermissionDeniedError("p")) == "llm_other"


def test_llm_error_type_fallback():
    """无 status_code 且类名无关键词 → llm_other 兜底（白名单内）。"""
    assert obs_mod.llm_error_type(ValueError("boom")) == "llm_other"


# ==================== middleware._obs_finish / dispatch ====================


def _obs_end_recorder(monkeypatch):
    calls: list[tuple] = []

    def rec(status, **kw):
        calls.append((status, kw))

    monkeypatch.setattr(mw, "obs_end", rec)
    return calls


def test_obs_finish_mapping(monkeypatch):
    """_obs_finish：断连 > LLM 硬失败 > HTTP_{code} > ok（仿 cs _obs_end）。"""
    calls = _obs_end_recorder(monkeypatch)
    mw._obs_finish(SimpleNamespace(status_code=200))
    mw._obs_finish(SimpleNamespace(status_code=503))
    mw._obs_finish(SimpleNamespace(status_code=200), aborted=True)
    mw._obs_finish(SimpleNamespace(status_code=200),
                   health={"hard_fail": True, "error_type": "llm_timeout"})
    assert calls == [
        ("ok", {}),
        ("error", {"error_type": "HTTP_503"}),
        ("error", {"error_type": "CLIENT_DISCONNECT", "error_msg": "客户端连接中断"}),
        ("error", {"error_type": "llm_timeout",
                   "error_msg": "LLM 调用失败，用户本轮未拿到正常回答"}),
    ]


def _make_request(path: str, rid: str | None = None) -> Request:
    headers = []
    if rid:
        headers.append((b"x-request-id", rid.encode()))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }
    return Request(scope)


@pytest.mark.asyncio
async def test_middleware_exempt_path_no_span(monkeypatch):
    """豁免路径（health/ready）→ 不建 span 不收口；X-Request-ID 头仍回传（链路不丢）。"""
    calls = _obs_end_recorder(monkeypatch)
    begun: list = []
    monkeypatch.setattr(mw, "obs_begin", lambda **kw: begun.append(kw))
    inst = mw.RequestIDMiddleware.__new__(mw.RequestIDMiddleware)

    async def _call_next(req):
        return PlainTextResponse("ok")

    resp = await inst.dispatch(_make_request("/health/ready", rid="rid-exempt"), _call_next)
    assert begun == [] and calls == []
    assert resp.headers["X-Request-ID"] == "rid-exempt"


@pytest.mark.asyncio
async def test_middleware_plain_ok_roundtrip(monkeypatch):
    """非豁免普通响应 → begin(method/path/trace_id=rid) 后收口 ok，rid 回传。"""
    calls = _obs_end_recorder(monkeypatch)
    begun: list = []
    monkeypatch.setattr(mw, "obs_begin", lambda **kw: begun.append(kw))
    inst = mw.RequestIDMiddleware.__new__(mw.RequestIDMiddleware)

    async def _call_next(req):
        return PlainTextResponse("ok")

    resp = await inst.dispatch(_make_request("/api/v1/reviews/1", rid="rid-abc"), _call_next)
    assert begun == [{"method": "GET", "path": "/api/v1/reviews/1", "trace_id": "rid-abc"}]
    assert calls == [("ok", {})]
    assert resp.headers["X-Request-ID"] == "rid-abc"


@pytest.mark.asyncio
async def test_middleware_streaming_end_after_body(monkeypatch):
    """SSE 流式：call_next 返回时未收口，body 迭代完成后才 end ok（覆盖流内 llm_call）。"""
    calls = _obs_end_recorder(monkeypatch)
    begun: list = []
    monkeypatch.setattr(mw, "obs_begin", lambda **kw: begun.append(kw))
    inst = mw.RequestIDMiddleware.__new__(mw.RequestIDMiddleware)

    async def _inner():
        yield b"chunk1"
        yield b"chunk2"

    async def _call_next(req):
        return StreamingResponse(_inner(), media_type="text/plain")

    resp = await inst.dispatch(_make_request("/api/v1/chat/stream"), _call_next)
    assert begun and calls == []  # dispatch 返回时 body 未消费 → 未收口
    body = b"".join([c async for c in resp.body_iterator])
    assert body == b"chunk1chunk2"
    assert calls == [("ok", {})]


@pytest.mark.asyncio
async def test_middleware_streaming_abort_disconnect(monkeypatch):
    """body 迭代中途异常（客户端断连）→ CLIENT_DISCONNECT 并重抛（trace 如实反映未完整）。"""
    calls = _obs_end_recorder(monkeypatch)
    begun: list = []
    monkeypatch.setattr(mw, "obs_begin", lambda **kw: begun.append(kw))
    inst = mw.RequestIDMiddleware.__new__(mw.RequestIDMiddleware)

    async def _inner():
        yield b"partial"
        raise RuntimeError("client gone")

    async def _call_next(req):
        return StreamingResponse(_inner(), media_type="text/plain")

    resp = await inst.dispatch(_make_request("/api/v1/chat/stream"), _call_next)
    assert calls == []
    with pytest.raises(RuntimeError):
        async for _ in resp.body_iterator:
            pass
    assert calls == [("error", {"error_type": "CLIENT_DISCONNECT",
                                "error_msg": "客户端连接中断"})]


@pytest.mark.asyncio
async def test_middleware_call_next_exception(monkeypatch):
    """call_next 抛异常未达响应 → UNHANDLED_EXCEPTION 收口并重抛。"""
    calls = _obs_end_recorder(monkeypatch)
    begun: list = []
    monkeypatch.setattr(mw, "obs_begin", lambda **kw: begun.append(kw))
    inst = mw.RequestIDMiddleware.__new__(mw.RequestIDMiddleware)

    async def _call_next(req):
        raise ValueError("handler crash")

    with pytest.raises(ValueError):
        await inst.dispatch(_make_request("/api/v1/reviews"), _call_next)
    assert begun  # 已建 span
    assert calls == [("error", {"error_type": "UNHANDLED_EXCEPTION",
                                "error_msg": "请求处理抛异常未达响应"})]


# ==================== deepseek_client 统一层出口打点 ====================


class _FakeCircuit:
    async def acquire(self):
        pass

    async def record_success(self):
        pass

    async def record_failure(self):
        pass


class _Delta:
    """production 无条件访问 delta.tool_calls（chat_stream_agent），非 None 才行。"""

    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _Choice:
    def __init__(self, delta, finish_reason=None):
        self.delta = delta
        self.finish_reason = finish_reason


class _Chunk:
    def __init__(self, choices=None, usage=None):
        self.choices = choices
        self.usage = usage


def _open_llm_gate(monkeypatch):
    """deepseek_client 出口断言：替换其绑定的 ok/error helper 别名。

    必须两个独立 mock——deepseek_client 在 import 时 `from app.obs import ... as _obs_llm_ok /
    _obs_llm_error` 绑定模块别名，patch 须落在 deepseek_client 模块名上；同一对象会被 ok 与
    error 共享 call_count 导致断言失真。
    """
    ok_mock = MagicMock()
    err_mock = MagicMock()
    monkeypatch.setattr(dc, "_obs_llm_ok", ok_mock)
    monkeypatch.setattr(dc, "_obs_llm_error", err_mock)
    monkeypatch.setattr(dc.settings, "deepseek_enabled", True)
    return ok_mock, err_mock


def _mk_instance(monkeypatch, create):
    """__new__ 直构 DeepSeekClient 避真实 OpenAI client/config（对齐 test_agent_loop）。"""
    inst = dc.DeepSeekClient.__new__(dc.DeepSeekClient)
    inst._circuit = _FakeCircuit()
    inst._client = MagicMock()
    inst._client.chat.completions.create = create
    return inst


def _recorder():
    out: list[tuple] = []

    def rec(*a, **kw):
        out.append((a, kw))

    return out, rec


def _err(status_code=None):
    """带/不带 status_code 的 fake openai 异常（分类走 app.obs.llm_error_type）。"""
    e = _StatusErr("upstream fail")
    if status_code is not None:
        e.status_code = status_code
    return e


@pytest.mark.asyncio
async def test_chat_stream_ok_records_usage(monkeypatch):
    """chat_stream 流末 usage chunk → record_llm ok with usage；流完成后单次收口。"""
    ok_mock, err_mock = _open_llm_gate(monkeypatch)

    async def _stream():
        yield _Chunk(choices=[_Choice(_Delta(content="评审"))])
        yield _Chunk(usage=_Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15))

    async def _create(**kw):
        return _stream()

    inst = _mk_instance(monkeypatch, _create)
    got = [(t, u) async for t, u in inst.chat_stream(
        [{"role": "user", "content": "hi"}], temperature=0.3, max_tokens=2048)]
    assert got[0][0] == "评审"
    assert got[1][1] == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    assert ok_mock.call_count == 1
    # 生产调用 `_obs_llm_ok(_obs_started, _obs_usage)`——usage 是第 2 个位置参
    assert ok_mock.call_args.args[1] == \
        {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    assert err_mock.call_count == 0


@pytest.mark.asyncio
async def test_chat_stream_ok_recorded_when_consumer_closes_midstream(monkeypatch):
    """消费方中途弃用（aclose()）：ok 收口仍须记账。

    GeneratorExit 承 BaseException，except Exception 接不住 ⇒ 写在流末的收口会静默全丢。
    **真机对照**（gq 侧 2026-09-15，三仓同形缺陷）：截断驱动零 llm_call、完整 drain 才有。
    本仓用 except GeneratorExit 而**不用 finally** —— 该 try 被 while 重试环包住，finally 会
    在每次试次都触发，把「试次失败待重试」记成 ok。
    """
    ok_mock, err_mock = _open_llm_gate(monkeypatch)

    async def _stream():
        yield _Chunk(choices=[_Choice(_Delta(content="评审"))])
        yield _Chunk(usage=_Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15))

    async def _create(**kw):
        return _stream()

    inst = _mk_instance(monkeypatch, _create)
    gen = inst.chat_stream(
        [{"role": "user", "content": "hi"}], temperature=0.3, max_tokens=2048)
    first = await gen.__anext__()
    assert first[0] == "评审", "只驱动一步 = 断连现场"
    await gen.aclose()

    assert ok_mock.call_count == 1, "一次调用一条账（既不能丢，也不能 finally 逐试次重复记）"
    assert err_mock.call_count == 0, "弃用不是错误，不得记 error"
    assert ok_mock.call_args.args[1] is None, "断连早于 usage chunk ⇒ usage 空，但状态仍为 ok"


@pytest.mark.asyncio
async def test_chat_stream_agent_ok_recorded_when_consumer_closes_midstream(monkeypatch):
    """chat_stream_agent 的弃用收口——第二处 `except GeneratorExit` 的独立护栏。

    与 chat_stream 是**两个独立烤点**（各自一个 except 块）：只驱动 chat_stream 的用例对
    本方法**零判别力**（实测删掉本处的 except 块、全量 365 仍全绿）。故单列一条。
    """
    ok_mock, err_mock = _open_llm_gate(monkeypatch)

    async def _stream():
        yield _Chunk(choices=[_Choice(_Delta(content="决策"))])
        yield _Chunk(usage=_Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15))

    async def _create(**kw):
        return _stream()

    inst = _mk_instance(monkeypatch, _create)
    gen = inst.chat_stream_agent(
        [{"role": "user", "content": "hi"}], [], temperature=0.3, max_tokens=2048)
    first = await gen.__anext__()
    assert first == {"type": "content", "delta": "决策"}, "只驱动一步 = 断连现场"
    await gen.aclose()

    assert ok_mock.call_count == 1, "一次调用一条账"
    assert err_mock.call_count == 0, "弃用不是错误"
    assert ok_mock.call_args.args[1] is None, "断连早于 usage chunk ⇒ usage 空，状态仍为 ok"


@pytest.mark.parametrize("method", ["chat_stream", "chat_stream_agent"])
@pytest.mark.asyncio
async def test_stream_abandoned_during_retry_backoff_records_error(monkeypatch, method):
    """退避等待期被取消（第二条黑洞出口）：仍须落一条 llm_call，不得整条消失。

    与上面两条的出口**不同**：那两条发生在 try 体里的 yield 点（可触发 GeneratorExit）；
    本条发生在本处理器的 `await asyncio.sleep(backoff)` 上——此时生成器**正在执行**，
    `aclose()` 会报 already running，真正的出口是**任务取消**（CancelledError）。而兄弟
    except 子句**不覆盖本处理器内抛出的异常** ⇒ 没有内层守卫时它会经 while 环直接冲出
    生成器：既无 ok 也无 error（`record_failure` 已先记 ⇒ 账目半截）。
    """
    ok_mock, err_mock = _open_llm_gate(monkeypatch)
    first_err = _err(503)

    async def _create(**kw):
        raise first_err

    _never = asyncio.Event()

    class _FakeAsyncio:
        # 本桩只替换 dc 模块内的 asyncio 引用，**必须把该模块还用到的属性一并代理**——
        # 漏了就变成「测试桩把生产代码弄崩」（首跑即踩：生产代码取 asyncio.CancelledError
        # 拿到 AttributeError）。此处 dc 只用 sleep 与 CancelledError 两个名字。
        CancelledError = asyncio.CancelledError

        @staticmethod
        async def sleep(_delay):
            await _never.wait()  # 永不返回 ⇒ 生成器停在退避等待处

    monkeypatch.setattr(dc, "asyncio", _FakeAsyncio)
    inst = _mk_instance(monkeypatch, _create)
    msgs = [{"role": "user", "content": "hi"}]
    # 两个流式方法各有**独立**一处守卫 ⇒ 必须各自驱动，否则删掉 agent 侧那处照样全绿
    if method == "chat_stream":
        gen = inst.chat_stream(msgs, temperature=0.3, max_tokens=2048)
    else:
        gen = inst.chat_stream_agent(msgs, [], temperature=0.3, max_tokens=2048)
    task = asyncio.create_task(gen.__anext__())
    for _ in range(5):  # 推进到退避等待（create 抛错 → 记熔断 → sleep）
        await asyncio.sleep(0)
    assert not task.done(), "前提：生成器确已停在退避等待，而非提前结束"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert ok_mock.call_count == 0, "本调用一次都没成功 ⇒ 记 ok 是假成功"
    assert err_mock.call_count == 1, "退避期被取消仍须落一条账（否则整条 llm_call 消失）"
    assert err_mock.call_args.args[1] is first_err, "按最后一次失败记 error"


@pytest.mark.asyncio
async def test_chat_ok_records_usage(monkeypatch):
    """chat（非流式）：resp.usage → ok with usage；返回正文。"""
    ok_mock, err_mock = _open_llm_gate(monkeypatch)

    async def _create(**kw):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="结论"))],
            usage=_Usage(prompt_tokens=8, completion_tokens=3, total_tokens=11),
        )

    inst = _mk_instance(monkeypatch, _create)
    out = await inst.chat([{"role": "user", "content": "q"}], temperature=0.3, max_tokens=2048)
    assert out == "结论"
    assert ok_mock.call_count == 1
    assert ok_mock.call_args.args[1] == \
        {"prompt_tokens": 8, "completion_tokens": 3, "total_tokens": 11}
    assert err_mock.call_count == 0


@pytest.mark.asyncio
async def test_chat_error_records_then_raises(monkeypatch):
    """chat 不重试类失败（401 schedule=()）：首败即先记 error 再抛（§2.4 先记再抛）。"""
    ok_mock, err_mock = _open_llm_gate(monkeypatch)

    async def _create(**kw):
        raise _err(status_code=401)

    inst = _mk_instance(monkeypatch, _create)
    with pytest.raises(_StatusErr):
        await inst.chat([{"role": "user", "content": "q"}], temperature=0.3, max_tokens=2048)
    assert ok_mock.call_count == 0
    assert err_mock.call_count == 1
    # 最终上抛的异常原样传给 helper；error_type 由 app.obs.record_llm_error 内部再分类
    assert isinstance(err_mock.call_args.args[1], _StatusErr)


@pytest.mark.asyncio
async def test_chat_disabled_no_recording(monkeypatch):
    """停用（deepseek_enabled=False）→ CircuitOpenError 前置拒绝，不打点（request 503 已反映）。"""
    ok_mock, err_mock = _open_llm_gate(monkeypatch)
    monkeypatch.setattr(dc.settings, "deepseek_enabled", False)

    async def _create(**kw):
        raise AssertionError("不应真正发起调用")

    inst = _mk_instance(monkeypatch, _create)
    with pytest.raises(dc.CircuitOpenError):
        await inst.chat([{"role": "user", "content": "q"}], temperature=0.3, max_tokens=2048)
    assert ok_mock.call_count == 0
    assert err_mock.call_count == 0


@pytest.mark.asyncio
async def test_chat_ok_recorded_when_cancelled_on_record_success(monkeypatch):
    """chat 取消窗口之一（窄窗）：响应已到手、取消落在 record_success 的 Lock 上 ⇒ 仍须记 ok。

    成功收口点 `_obs_llm_ok` 排在 `await self._circuit.record_success()` **之后** ⇒ 不补守卫时，
    这条**已成功**的调用零账面。与流式侧「弃用丢 ok」同因，但出口机制不同：协程没有
    `aclose()`/GeneratorExit 那条，只能由任务取消触发（本用例即该出口的单测复现）。
    """
    ok_mock, err_mock = _open_llm_gate(monkeypatch)
    never = asyncio.Event()

    class _SlowCircuit(_FakeCircuit):
        async def record_success(self):
            await never.wait()  # 永不返回 ⇒ 取消必然落在本 await 上

    async def _create(**kw):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="结论"))],
            usage=_Usage(prompt_tokens=8, completion_tokens=3, total_tokens=11),
        )

    inst = _mk_instance(monkeypatch, _create)
    inst._circuit = _SlowCircuit()
    task = asyncio.create_task(
        inst.chat([{"role": "user", "content": "q"}], temperature=0.3, max_tokens=2048)
    )
    for _ in range(5):  # 推进到 record_success 的 await
        await asyncio.sleep(0)
    assert not task.done(), "前提：生成确实停在 record_success，而非提前结束"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert ok_mock.call_count == 1, "调用已成功 ⇒ 取消不得让它零账面"
    assert ok_mock.call_args.args[1] == \
        {"prompt_tokens": 8, "completion_tokens": 3, "total_tokens": 11}
    assert err_mock.call_count == 0


@pytest.mark.asyncio
async def test_chat_abandoned_during_retry_backoff_records_error(monkeypatch):
    """chat 取消窗口之二（宽窗）：退避等待期被取消 ⇒ 仍须落一条 error。

    与流式侧 `test_stream_abandoned_during_retry_backoff_records_error` 同形（同一段退避、同一段
    sleep，窗口 0.5~4s）：处理器内抛出的异常不被兄弟 except 子句接住 ⇒ 直接冲出函数，既无 ok 也
    无 error。**本处与上面「record_success 被取消」是两个独立悬点，必须各自驱动**——只驱动一处时，
    删掉另一处的守卫全量仍全绿（本批在流式侧已踩过一次）。
    """
    ok_mock, err_mock = _open_llm_gate(monkeypatch)
    first_err = _err(503)

    async def _create(**kw):
        raise first_err

    _never = asyncio.Event()

    class _FakeAsyncio:
        # 同流式侧：桩替换 dc 模块内的 asyncio 引用，**必须把该模块还用到的属性一并代理**
        # （dc 只用到 sleep 与 CancelledError 两个名字）。
        CancelledError = asyncio.CancelledError

        @staticmethod
        async def sleep(_delay):
            await _never.wait()  # 永不返回 ⇒ 停在退避等待处

    monkeypatch.setattr(dc, "asyncio", _FakeAsyncio)
    inst = _mk_instance(monkeypatch, _create)
    task = asyncio.create_task(
        inst.chat([{"role": "user", "content": "q"}], temperature=0.3, max_tokens=2048)
    )
    for _ in range(5):  # 推进到退避等待（create 抛错 → 取 schedule → sleep）
        await asyncio.sleep(0)
    assert not task.done(), "前提：确实停在退避等待，而非提前结束"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert ok_mock.call_count == 0, "本调用一次都没成功 ⇒ 记 ok 是假成功"
    assert err_mock.call_count == 1, "退避期被取消仍须落一条账（否则整条 llm_call 消失）"
    assert err_mock.call_args.args[1] is first_err, "按最后一次失败记 error"


@pytest.mark.asyncio
async def test_chat_stream_agent_ok_records_usage(monkeypatch):
    """chat_stream_agent：流末 usage → record_llm ok（agent 决策轮同走统一层出口）。"""
    ok_mock, err_mock = _open_llm_gate(monkeypatch)

    async def _stream():
        yield _Chunk(choices=[_Choice(_Delta(content="<thinking>x</thinking>"))])
        yield _Chunk(usage=_Usage(prompt_tokens=20, completion_tokens=6, total_tokens=26))

    async def _create(**kw):
        return _stream()

    inst = _mk_instance(monkeypatch, _create)
    events = [e async for e in inst.chat_stream_agent(
        [{"role": "user", "content": "决策"}], [{"type": "function", "function": {"name": "f"}}],
        temperature=0.3, max_tokens=2048)]
    assert any(e["type"] == "usage" for e in events)
    assert ok_mock.call_count == 1
    assert ok_mock.call_args.args[1]["total_tokens"] == 26
    assert err_mock.call_count == 0


# ==================== summarize 旁路（自建 AsyncOpenAI）打点 ====================


def _patch_summarize_obs(monkeypatch):
    """_summarize_with_llm 每次调用内 `from app.obs import ...`，patch app.obs 属性即可拦截。"""
    ok_calls, ok_rec = _recorder()
    err_calls, err_rec = _recorder()
    monkeypatch.setattr(obs_mod, "record_llm_ok", ok_rec)
    monkeypatch.setattr(obs_mod, "record_llm_error", err_rec)
    return ok_calls, err_calls


@pytest.mark.asyncio
async def test_summarize_ok_records_usage(monkeypatch):
    """旁路成功 → record_llm ok with usage（r.usage.model_dump()），返回摘要正文。"""
    ok_calls, err_calls = _patch_summarize_obs(monkeypatch)
    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(
            create=AsyncMock(return_value=SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="压缩后的摘要"))],
                usage=_Usage(prompt_tokens=30, completion_tokens=9, total_tokens=39),
            ))
        ))
    )
    monkeypatch.setattr("openai.AsyncOpenAI", lambda **kw: fake_client)

    stage = [SimpleNamespace(role="user", content="问题1"),
             SimpleNamespace(role="assistant", content="答1")]
    out = await _summarize_with_llm(stage)
    assert out == "压缩后的摘要"
    assert len(ok_calls) == 1 and err_calls == []
    # 生产调用 `_obs_ok(_obs_started, r.usage.model_dump())`——usage 是第 2 个位置参
    assert ok_calls[0][0][1] == {"prompt_tokens": 30, "completion_tokens": 9, "total_tokens": 39}


@pytest.mark.asyncio
async def test_summarize_error_records_then_fallback(monkeypatch):
    """旁路失败 → 先记 error 再吞异常返 None（调用方原文兜底，不阻断对话）。"""
    ok_calls, err_calls = _patch_summarize_obs(monkeypatch)
    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(
            create=AsyncMock(side_effect=_err())  # LLM 不可用/超时
        ))
    )
    monkeypatch.setattr("openai.AsyncOpenAI", lambda **kw: fake_client)

    out = await _summarize_with_llm([SimpleNamespace(role="user", content="问题1")])
    assert out is None
    assert ok_calls == [] and len(err_calls) == 1
    assert isinstance(err_calls[0][0][1], _StatusErr)  # 原始异常传给 helper（分类在 app.obs 内）

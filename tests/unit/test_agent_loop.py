"""P7.x agent 编排单元测试（agent_loop.py + deepseek_client.chat_stream_agent）。

覆盖：
- stream_agent 事件序：LLM 调工具命中→[tool_call, answer]、直接答→[answer]、
  F3 规则否决→[tool_call(rule_override), answer]（round2 带 override prompt）、
  检索空→三路兜底"未找到"（round2 不调 LLM）、round1/round2 失败→[error]
- 决策轮 content 丢弃（只作答轮进 answer，保三发对齐契约）
- deepseek_client.chat_stream_agent：tool_calls 增量累加（arguments 拼接）、
  finish_reason=="tool_calls" 单次 flush、content/usage 事件
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

import app.ai.agent.agent_loop as agent
import app.ai.llm.deepseek_client as dc
from app.ai.agent import CHAT_TOOLS, ToolContext, stream_agent
from app.ai.agent.agent_loop import _NOT_FOUND_ANSWER


# ==================== mock helpers ====================


def _round1_gen(events):
    async def _round1(messages, tools, **kw):
        for ev in events:
            yield ev

    return _round1


def _round2_gen(deltas, captured=None, exc=None):
    async def _round2(messages, **kw):
        if captured is not None:
            captured["msgs"] = messages
        if exc:
            raise exc
        for d in deltas:
            yield d, None

    return _round2


def _client(round1_events, round2_deltas=None, captured=None, round2_exc=None):
    client = MagicMock()
    client.chat_stream_agent = _round1_gen(round1_events)
    if round2_deltas is not None or round2_exc is not None or captured is not None:
        client.chat_stream = _round2_gen(round2_deltas or [], captured, round2_exc)
    return client


def _ctx():
    return ToolContext(session=MagicMock(), review=MagicMock(), bid=MagicMock())


def _messages(question):
    return [{"role": "system", "content": "s"}, {"role": "user", "content": question}]


# ==================== stream_agent 事件序 ====================


@pytest.mark.asyncio
async def test_agent_tool_hit(monkeypatch):
    """LLM 调 retrieve_knowledge 命中：先 tool_call 决策帧、再 round2 作答帧；决策轮 content 丢弃。"""
    events = [
        {"type": "content", "delta": "<thinking>需要检索标书</thinking>"},  # 决策轮思考，丢弃
        {"type": "usage", "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}},
        {"type": "tool_call", "tool_calls": [
            {"id": "t1", "type": "function",
             "function": {"name": "retrieve_knowledge", "arguments": '{"query": "技术方案"}'}},
        ]},
    ]
    captured = {}
    client = _client(events, round2_deltas=["<thinking>基于标书</thinking>", "<answer>方案完整可行</answer>"],
                     captured=captured)
    monkeypatch.setattr(agent, "get_client", lambda: client)

    async def _fake_retrieve(ctx, query):
        return {"source_count": 2, "context": "[来源1] 微服务\n[来源2] 分层",
                "confidence_band": "high", "max_score": 0.8}

    monkeypatch.setattr(agent, "execute_retrieve_knowledge", _fake_retrieve)
    got = [e async for e in stream_agent(_messages("这个标书的技术方案怎么样？"), CHAT_TOOLS, _ctx())]
    assert [e["type"] for e in got] == ["tool_call", "answer"]
    assert got[0]["name"] == "retrieve_knowledge"
    assert got[0]["status"] == "success"
    assert got[0]["result"]["source_count"] == 2
    # 作答帧：round2 完整 thinking+answer，不含决策轮"需要检索标书"思考（三发对齐约束）
    assert "方案完整可行" in got[1]["text"]
    assert "需要检索标书" not in got[1]["text"]
    # round2 走 chat_stream 不带 tools（防再循环）
    assert "tools" not in captured["msgs"][-2]
    assert captured["msgs"][-2]["role"] == "assistant"
    assert captured["msgs"][-1]["role"] == "tool"
    assert captured["msgs"][-1]["tool_call_id"] == "t1"
    # usage 合并 round1
    assert got[1]["usage"]["prompt_tokens"] == 10


@pytest.mark.asyncio
async def test_agent_direct_answer(monkeypatch):
    """LLM 直接答（小闲聊/明确不需要工具）：round1 content 即作答帧，无 tool_call。"""
    events = [
        {"type": "content", "delta": "<thinking>问候</thinking>"},
        {"type": "content", "delta": "<answer>你好，请问有什么可以帮您</answer>"},
    ]
    client = _client(events)
    monkeypatch.setattr(agent, "get_client", lambda: client)
    got = [e async for e in stream_agent(_messages("你好"), CHAT_TOOLS, _ctx())]
    assert [e["type"] for e in got] == ["answer"]
    assert "<answer>你好，请问有什么可以帮您</answer>" in got[0]["text"]


@pytest.mark.asyncio
async def test_agent_f3_rule_override(monkeypatch):
    """F3 规则否决：LLM 未调工具但规则判 query → 强制检索，round2 走 user 消息带 context 重答。"""
    events = [
        {"type": "content", "delta": "<thinking>直接回答</thinking>"},
        {"type": "content", "delta": "<answer>技术方案合理</answer>"},
    ]
    captured = {}
    client = _client(events, round2_deltas=["<thinking>基于检索</thinking><answer>方案有据</answer>"],
                     captured=captured)
    monkeypatch.setattr(agent, "get_client", lambda: client)

    async def _fake_retrieve(ctx, query):
        return {"source_count": 1, "context": "[来源1] 标书技术方案", "confidence_band": "high"}

    monkeypatch.setattr(agent, "execute_retrieve_knowledge", _fake_retrieve)
    got = [e async for e in stream_agent(_messages("这个标书的技术方案怎么样？"), CHAT_TOOLS, _ctx())]
    assert [e["type"] for e in got] == ["tool_call", "answer"]
    assert got[0]["status"] == "rule_override"
    # round2 messages 含 override prompt 的 user 消息（无 tool_calls 不能走 tool 消息回传）
    assert any("已为你检索到以下标书内容" in m.get("content", "") for m in captured["msgs"])
    assert "方案有据" in got[1]["text"]


@pytest.mark.asyncio
async def test_agent_rule_override_disabled(monkeypatch):
    """agent_rule_override_enabled=False：LLM 未调工具且规则判 query → 直接答（整条否决关闭）。"""
    events = [
        {"type": "content", "delta": "<thinking>直接回答</thinking>"},
        {"type": "content", "delta": "<answer>技术方案合理</answer>"},
    ]
    client = _client(events)
    monkeypatch.setattr(agent, "get_client", lambda: client)
    monkeypatch.setattr(agent.settings, "agent_rule_override_enabled", False)
    got = [e async for e in stream_agent(_messages("这个标书的技术方案怎么样？"), CHAT_TOOLS, _ctx())]
    assert [e["type"] for e in got] == ["answer"]
    client.chat_stream.assert_not_called()  # 无强制检索 round2


@pytest.mark.asyncio
async def test_agent_referential_history(monkeypatch):
    """回指词 + ctx.history：unknown 短词（就这个）延续上一轮 query → F3 强制检索（非澄清）。"""
    events = [
        {"type": "content", "delta": "<thinking>直接回答</thinking>"},
        {"type": "content", "delta": "<answer>就这个</answer>"},
    ]
    captured = {}
    client = _client(events, round2_deltas=["<thinking>基于检索</thinking><answer>技术方案有据</answer>"],
                     captured=captured)
    monkeypatch.setattr(agent, "get_client", lambda: client)

    async def _fake_retrieve(ctx, query):
        return {"source_count": 1, "context": "[来源1] 技术方案", "confidence_band": "high"}

    monkeypatch.setattr(agent, "execute_retrieve_knowledge", _fake_retrieve)
    ctx = _ctx()
    ctx.history = ["这个标书的技术方案怎么样？"]
    got = [e async for e in stream_agent(_messages("就这个"), CHAT_TOOLS, ctx)]
    # 归队 query → F3 强制检索（rule_override），而非 unknown 走澄清话术
    assert [e["type"] for e in got] == ["tool_call", "answer"]
    assert got[0]["status"] == "rule_override"
    assert got[0]["intent"] == "query"


@pytest.mark.asyncio
async def test_agent_retrieve_empty_not_found(monkeypatch):
    """检索空 + query 意图：三路兜底"未找到"固定话术，不调 round2 LLM（防空 context 编造）。"""
    events = [
        {"type": "tool_call", "tool_calls": [
            {"id": "t1", "type": "function",
             "function": {"name": "retrieve_knowledge", "arguments": '{"query": "不存在"}'}},
        ]},
    ]
    client = _client(events)
    monkeypatch.setattr(agent, "get_client", lambda: client)

    async def _fake_retrieve(ctx, query):
        return {"source_count": 0, "context": "", "confidence_band": "none"}

    monkeypatch.setattr(agent, "execute_retrieve_knowledge", _fake_retrieve)
    got = [e async for e in stream_agent(_messages("这个标书有没有备份容灾方案？"), CHAT_TOOLS, _ctx())]
    assert [e["type"] for e in got] == ["tool_call", "answer"]
    assert got[0]["status"] == "success"
    assert got[1]["text"] == _NOT_FOUND_ANSWER
    client.chat_stream.assert_not_called()  # round2 不调 LLM


@pytest.mark.asyncio
async def test_agent_smalltalk_retrieve_empty_llm(monkeypatch):
    """检索空但 smalltalk/非文档问题：round2 LLM 自然引导（不走"未找到"）。"""
    events = [
        {"type": "tool_call", "tool_calls": [
            {"id": "t1", "type": "function",
             "function": {"name": "retrieve_knowledge", "arguments": '{"query": "计算"}'}},
        ]},
    ]
    client = _client(events, round2_deltas=["<answer>这是一个简单的乘法问题，答案是 391。</answer>"])
    monkeypatch.setattr(agent, "get_client", lambda: client)

    async def _fake_retrieve(ctx, query):
        return {"source_count": 0, "context": "", "confidence_band": "none"}

    monkeypatch.setattr(agent, "execute_retrieve_knowledge", _fake_retrieve)
    got = [e async for e in stream_agent(_messages("17×23等于多少"), CHAT_TOOLS, _ctx())]
    assert got[1]["text"] == "<answer>这是一个简单的乘法问题，答案是 391。</answer>"


@pytest.mark.asyncio
async def test_agent_round1_error(monkeypatch):
    """round1 LLM 失败：error 帧（断路器/重试耗尽）。"""
    async def _round1(messages, tools, **kw):
        raise RuntimeError("llm down")

    client = MagicMock()
    client.chat_stream_agent = _round1
    monkeypatch.setattr(agent, "get_client", lambda: client)
    got = [e async for e in stream_agent(_messages("技术方案怎么样"), CHAT_TOOLS, _ctx())]
    assert [e["type"] for e in got] == ["error"]


@pytest.mark.asyncio
async def test_agent_round2_error(monkeypatch):
    """round2 作答 LLM 失败：error 帧（tool 决策已执行，作答失败）。"""
    events = [
        {"type": "tool_call", "tool_calls": [
            {"id": "t1", "type": "function",
             "function": {"name": "retrieve_knowledge", "arguments": '{"query": "技术方案"}'}},
        ]},
    ]
    client = _client(events, round2_exc=RuntimeError("round2 boom"))
    monkeypatch.setattr(agent, "get_client", lambda: client)

    async def _fake_retrieve(ctx, query):
        return {"source_count": 1, "context": "[来源1] x", "confidence_band": "high"}

    monkeypatch.setattr(agent, "execute_retrieve_knowledge", _fake_retrieve)
    got = [e async for e in stream_agent(_messages("技术方案怎么样"), CHAT_TOOLS, _ctx())]
    assert [e["type"] for e in got] == ["tool_call", "error"]


# ==================== deepseek_client.chat_stream_agent tool_calls 解析 ====================


class _Fn:
    def __init__(self, name=None, arguments=None):
        self.name = name
        self.arguments = arguments


class _ToolCall:
    def __init__(self, index, id=None, name=None, arguments=None):
        self.index = index
        self.id = id
        self.function = _Fn(name, arguments)


class _Delta:
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


class _FakeCircuit:
    async def acquire(self):
        pass

    async def record_success(self):
        pass

    async def record_failure(self):
        pass


@pytest.mark.asyncio
async def test_chat_stream_agent_accumulates_tool_calls(monkeypatch):
    """chat_stream_agent：tool_calls 按 index 累加（arguments 增量拼接）、
    finish_reason=='tool_calls' 单次 flush、content/usage 事件透出。"""
    instance = dc.DeepSeekClient.__new__(dc.DeepSeekClient)
    instance._circuit = _FakeCircuit()
    instance._client = MagicMock()

    class _Usage:
        """openai SDK usage chunk 是 pydantic 对象（生产代码调 .model_dump()）。"""

        def __init__(self, **kw):
            self._d = kw

        def model_dump(self):
            return dict(self._d)

    chunks = [
        _Chunk(choices=[_Choice(_Delta(content="<thinking>先检索</thinking>"))]),
        _Chunk(choices=[_Choice(_Delta(tool_calls=[_ToolCall(0, id="t1", name="retrieve_knowledge",
                                                            arguments='{"query": "技术')]))]),
        _Chunk(choices=[_Choice(_Delta(tool_calls=[_ToolCall(0, arguments='方案"}')]))]),
        _Chunk(choices=[_Choice(_Delta(), "tool_calls")]),
        _Chunk(usage=_Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15)),
    ]

    async def _stream(**kw):
        for c in chunks:
            yield c

    # openai SDK create 为 async 方法：await 后返回 async generator；每次重试返回新 gen
    async def _create(**kw):
        return _stream()

    instance._client.chat.completions.create = _create
    monkeypatch.setattr(dc.settings, "deepseek_enabled", True)
    monkeypatch.setattr(dc.config_service, "get_sync", lambda k: 0.3 if "temp" in k else 2048)

    got = [e async for e in instance.chat_stream_agent(
        [{"role": "user", "content": "技术方案怎么样"}], [{"type": "function"}],
        temperature=0.3, max_tokens=2048)]
    tool_evs = [e for e in got if e["type"] == "tool_call"]
    assert len(tool_evs) == 1  # finish_reason 时 flush 一次
    tc = tool_evs[0]["tool_calls"][0]
    assert tc["id"] == "t1"
    assert tc["function"]["name"] == "retrieve_knowledge"
    assert tc["function"]["arguments"] == '{"query": "技术方案"}'
    assert any(e["type"] == "content" for e in got)
    assert any(e["type"] == "usage" for e in got)

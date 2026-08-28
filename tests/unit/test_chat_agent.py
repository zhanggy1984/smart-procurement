"""chat_agent 控制层门面单元测试（C 档分层 2026-08-28）。

覆盖：对话状态管理归位控制层后的事件流与落库语义——
- 正常流：tool_call → reasoning/answer 增量 → done；user 先落库、assistant 后落库
  （assistant 落库纯 answer，思考剥离不进对话历史）
- 失败流：stream_agent 产 error → 透出 error + 仍 yield done（契约收尾），assistant 不落库
- 空回复：answer 文本空 → 无 answer 增量，done.content 空

不依赖真实 DB/LLM：mock conversation_service 与 stream_agent（编排本身由
test_agent_loop.py 覆盖，这里只验证 chat_agent 的归位逻辑）。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import app.ai.agent.agent_loop as agent


def _review():
    r = MagicMock()
    r.review_id = "REV-1"
    r.dimension_id = "DIM-1"
    r.bid_id = "BID-1"
    r.expert_id = "EXP-1"
    return r


def _session():
    """session：get 返回 MagicMock（bid/dimension）；scalars().all() 返回 []（无历史 user 消息）。"""
    s = AsyncMock()
    s.scalars = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    s.get = AsyncMock(return_value=MagicMock())
    return s


def _patch_env(monkeypatch, stream_events, added=None):
    """mock conversation_service 三方法 + stream_agent 事件序列。"""
    added = [] if added is None else added

    async def _fake_add_message(session, *, review_id, dimension_id, role, content, **kw):
        added.append((role, content))
        return MagicMock()

    async def _fake_get_context(session, *, review_id, dimension_id, **kw):
        return "历史上下文"

    async def _fake_maybe_summarize(session, *, review_id, dimension_id):
        return None

    async def _fake_stream(prompt, tools, ctx):
        for ev in stream_events:
            yield ev

    monkeypatch.setattr(agent.conversation, "add_message", _fake_add_message)
    monkeypatch.setattr(agent.conversation, "get_context", _fake_get_context)
    monkeypatch.setattr(agent.conversation, "maybe_summarize", _fake_maybe_summarize)
    monkeypatch.setattr(agent, "stream_agent", _fake_stream)


# ==================== 正常流 ====================


@pytest.mark.asyncio
async def test_chat_agent_happy_path(monkeypatch):
    """LLM 调工具 + 作答：tool_call → reasoning/answer 增量 → done；assistant 落库纯 answer。"""
    added = []
    _patch_env(monkeypatch, [
        {"type": "tool_call", "name": "retrieve_knowledge", "args": {"query": "技术方案"},
         "result": {"source_count": 2}, "status": "success", "intent": "query"},
        {"type": "answer", "text": "<thinking>需要先检索</thinking><answer>方案完整可行</answer>",
         "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}},
    ], added)

    got = [ev async for ev in agent.chat_agent(_session(), review=_review(), question="技术方案怎么样？")]
    types = [e["type"] for e in got]
    assert types == ["tool_call", "reasoning", "answer", "done"]
    assert got[0]["name"] == "retrieve_knowledge"
    # 落库顺序：user 先（生成器开头）、assistant 后（流末尾 finally）
    assert [r for r, _ in added] == ["user", "assistant"]
    # assistant 落库纯 answer（思考剥离，避免污染对话历史/摘要）
    assert added[-1][1] == "方案完整可行"
    # done 携带 usage + 纯作答全文
    assert got[-1]["usage"]["prompt_tokens"] == 10
    assert got[-1]["content"] == "方案完整可行"
    # reasoning/answer 增量正确
    assert "".join(e["delta"] for e in got if e["type"] == "reasoning") == "需要先检索"
    assert "".join(e["delta"] for e in got if e["type"] == "answer") == "方案完整可行"


# ==================== 失败流 ====================


@pytest.mark.asyncio
async def test_chat_agent_error_emits_done(monkeypatch):
    """stream_agent 产 error：透出 error 仍 yield done（契约 usage/done 收尾），assistant 不落库。"""
    added = []
    _patch_env(monkeypatch, [
        {"type": "error", "message": "LLM 调用失败，请稍后重试"},
    ], added)

    got = [ev async for ev in agent.chat_agent(_session(), review=_review(), question="技术方案怎么样？")]
    types = [e["type"] for e in got]
    assert types == ["error", "done"]
    assert got[0]["message"] == "LLM 调用失败，请稍后重试"
    # full 空 → 只落 user，assistant 不落
    assert [r for r, _ in added] == ["user"]
    assert got[-1]["content"] == ""
    assert got[-1]["usage"]["total_tokens"] == 0


@pytest.mark.asyncio
async def test_chat_agent_empty_answer(monkeypatch):
    """作答文本空：无 answer 增量，done.content 空，assistant 不落库。"""
    added = []
    _patch_env(monkeypatch, [
        {"type": "answer", "text": "   ", "usage": {"prompt_tokens": 1, "completion_tokens": 0,
                                                    "total_tokens": 1}},
    ], added)

    got = [ev async for ev in agent.chat_agent(_session(), review=_review(), question="你好")]
    # done 收尾（splitter 对空白无标签文本可能双发 reasoning/answer 空白增量，不断言精确事件序）
    assert got[-1]["type"] == "done"
    assert got[-1]["content"].strip() == ""
    # 空白作答 → assistant 不落库
    assert [r for r, _ in added] == ["user"]


# ==================== 上下文组装 ====================


@pytest.mark.asyncio
async def test_chat_agent_uses_question_in_prompt(monkeypatch):
    """get_context 收到当前问题；stream_agent 收到 build_chat_prompt 产物（tools 声明）。"""
    captured = {}
    added = []

    async def _fake_add_message(session, *, review_id, dimension_id, role, content, **kw):
        added.append((role, content))
        return MagicMock()

    async def _fake_get_context(session, *, review_id, dimension_id, **kw):
        return "历史上下文"

    async def _fake_maybe_summarize(session, *, review_id, dimension_id):
        return None

    async def _fake_stream(prompt, tools, ctx):
        captured["prompt"] = prompt
        captured["tools"] = tools
        captured["ctx"] = ctx
        yield {"type": "error", "message": "x"}

    monkeypatch.setattr(agent.conversation, "add_message", _fake_add_message)
    monkeypatch.setattr(agent.conversation, "get_context", _fake_get_context)
    monkeypatch.setattr(agent.conversation, "maybe_summarize", _fake_maybe_summarize)
    monkeypatch.setattr(agent, "stream_agent", _fake_stream)

    got = [ev async for ev in agent.chat_agent(_session(), review=_review(), question="就这个")]
    assert got[-1]["type"] == "done"
    # user 消息先落库（回指 history 依赖它）
    assert added[0][0] == "user" and added[0][1] == "就这个"
    # prompt 含当前问题（build_chat_prompt 拼接）+ 工具声明（prompt 是 messages 列表）
    assert any("就这个" in m.get("content", "") for m in captured["prompt"])
    assert captured["tools"] is agent.CHAT_TOOLS
    # ctx 携带 review/dimension（回指归队依赖；dimension 来自 session.get mock，断言非空即可）
    assert captured["ctx"].review.review_id == "REV-1"
    assert captured["ctx"].dimension is not None

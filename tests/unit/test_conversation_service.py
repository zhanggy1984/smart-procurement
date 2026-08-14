"""P7.2 ConversationService 单元测试（task.md：4 用例）。

覆盖：token 计数、turn/dim_turn 递增（MESSAGE 推进、SUMMARY 只推进全局）、
摘要触发阈值（消息不足不触发）、LLM 失败兜底（返回 None 不阻断）。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.conversation_service import (
    SUMMARY_TRIGGER_TURN,
    _count_tokens,
    add_message,
    maybe_summarize,
)


def test_count_tokens():
    """tiktoken 计数：空串 0，中文按 token 计（>0）。"""
    assert _count_tokens("") == 0
    assert _count_tokens("你好世界") > 0


@pytest.mark.asyncio
async def test_add_message_turn_increment():
    """MESSAGE 推进全局 turn + 维度 dim_turn。"""
    session = AsyncMock()
    session.scalar.side_effect = [3, 2]  # _next_turn→3, _next_dim_turn→2
    with patch("app.services.conversation_service.generate_id", return_value="MSG-1"):
        msg = await add_message(session, review_id="REV-1", dimension_id="D1",
                                role="user", content="问题", message_type="MESSAGE")
    assert msg.turn_number == 4
    assert msg.dim_turn_number == 3


@pytest.mark.asyncio
async def test_add_message_summary_does_not_advance_dim_turn():
    """SUMMARY 只推进全局 turn，dim_turn 停在当前轮（不占对话轮）。"""
    session = AsyncMock()
    session.scalar.side_effect = [6, 3]
    with patch("app.services.conversation_service.generate_id", return_value="MSG-S"):
        msg = await add_message(session, review_id="REV-1", dimension_id="D1",
                                role="assistant", content="摘要", message_type="SUMMARY")
    assert msg.turn_number == 7
    # _next_dim_turn 返回 cur+1=4，SUMMARY 分支 -1 → 3（对齐阶段末尾，不占新轮次）
    assert msg.dim_turn_number == 3


@pytest.mark.asyncio
async def test_maybe_summarize_below_trigger():
    """消息数不足触发阈值 → 不摘要（返回 None）。"""
    session = AsyncMock()
    msgs = [MagicMock() for _ in range(SUMMARY_TRIGGER_TURN - 1)]
    result = MagicMock()  # ScalarResult.all() 是同步方法，不能用 AsyncMock
    result.all.return_value = msgs
    session.scalars.return_value = result
    assert await maybe_summarize(session, review_id="REV-1", dimension_id="D1") is None


@pytest.mark.asyncio
async def test_maybe_summarize_llm_failure_fallback():
    """达到阈值但 LLM 失败 → 返回 None（get_context 原文兜底，不阻断）。"""
    session = AsyncMock()
    msgs = [MagicMock(dim_turn_number=i + 1) for i in range(SUMMARY_TRIGGER_TURN)]
    result = MagicMock()  # ScalarResult.all() 是同步方法，不能用 AsyncMock
    result.all.return_value = msgs
    session.scalars.return_value = result
    # 阶段边界查询返回 0（无历史 SUMMARY）
    session.scalar.return_value = 0
    with patch("app.services.conversation_service._summarize_with_llm",
               new=AsyncMock(return_value=None)):
        out = await maybe_summarize(session, review_id="REV-1", dimension_id="D1")
    assert out is None

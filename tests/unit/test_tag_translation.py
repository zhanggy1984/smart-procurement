"""P7.2 标签翻译服务单元测试（task.md：P4.1 标签翻译 + 降级）。

覆盖：
- parse_tags：LLM 输出过滤到受控词表、去重保序、空输入
- translate_tags：命中词表 → AUTO；空描述 / 断路器 OPEN / 异常 / 未命中词表
  → MANUAL_TAG_SELECTION 降级（PM 手动选标签）
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.ai.llm.deepseek_client import CircuitOpenError
from app.services.tag_translation_service import (
    MODE_AUTO,
    MODE_MANUAL,
    parse_tags,
    translate_tags,
)


def test_parse_tags_filters_to_vocab():
    """仅保留受控词表内标签，词表外丢弃；多分隔符兼容。"""
    tags = parse_tags("软件开发，网络安全；云计算、软件开发")
    assert tags == ["软件开发", "网络安全", "云计算"]  # 去重保序
    assert parse_tags("火星标签，随便写的") == []  # 词表外全丢弃
    assert parse_tags("") == []
    assert parse_tags(None) == []


@pytest.mark.asyncio
async def test_translate_tags_auto():
    """LLM 输出命中词表 → AUTO 模式 + 词表内标签。"""
    client = MagicMock()
    client.chat = AsyncMock(return_value="软件开发、网络安全")
    with patch("app.services.tag_translation_service.get_client", return_value=client):
        tags, mode = await translate_tags("智慧校园平台建设项目")
    assert mode == MODE_AUTO
    assert "软件开发" in tags
    assert "网络安全" in tags


@pytest.mark.asyncio
async def test_translate_tags_circuit_open_degraded():
    """断路器 OPEN → 降级 MANUAL_TAG_SELECTION，不抛错。"""
    client = MagicMock()
    client.chat = AsyncMock(side_effect=CircuitOpenError("circuit open"))
    with patch("app.services.tag_translation_service.get_client", return_value=client):
        tags, mode = await translate_tags("智慧校园平台建设项目")
    assert tags == [] and mode == MODE_MANUAL


@pytest.mark.asyncio
async def test_translate_tags_llm_error_degraded():
    """LLM 网络异常 → 降级 MANUAL_TAG_SELECTION。"""
    client = MagicMock()
    client.chat = AsyncMock(side_effect=RuntimeError("llm down"))
    with patch("app.services.tag_translation_service.get_client", return_value=client):
        tags, mode = await translate_tags("智慧校园平台建设项目")
    assert tags == [] and mode == MODE_MANUAL


@pytest.mark.asyncio
async def test_translate_tags_empty_description():
    """空描述 → 直接降级（不调 LLM）。"""
    with patch("app.services.tag_translation_service.get_client") as gc:
        tags, mode = await translate_tags("   ")
    assert tags == [] and mode == MODE_MANUAL
    gc.assert_not_called()


@pytest.mark.asyncio
async def test_translate_tags_no_vocab_match():
    """LLM 输出未命中词表 → 降级（严格过滤，不伪造标签）。"""
    client = MagicMock()
    client.chat = AsyncMock(return_value="这不是词表内的标签")
    with patch("app.services.tag_translation_service.get_client", return_value=client):
        tags, mode = await translate_tags("智慧校园平台建设项目")
    assert tags == [] and mode == MODE_MANUAL

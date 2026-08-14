"""P7.2 BGE-M3 Embedder 单元测试（task.md：2 用例）。

覆盖编码维度=1024、批处理正确性（HTTP 模式，mock httpx + settings）。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.ai.rag.embedder import BGE3Embedder


def _mk_response_json(batch_texts: list[str]) -> dict:
    """bge-m3 容器响应契约：{"vectors": [[...]], "dim": 1024}。"""
    return {"vectors": [[float(i + 1)] + [0.0] * 1023 for i in range(len(batch_texts))], "dim": 1024}


@pytest.mark.asyncio
async def test_embed_dimension_and_batch_order():
    """HTTP 模式：1024 维 + 批序保留（mock settings.bge_m3_endpoint + httpx.post）。"""
    mock_post = AsyncMock(return_value=httpx.Response(
        200, json=_mk_response_json(["第一份标书", "第二份标书"]), request=httpx.Request("POST", "http://x"),
    ))
    with patch("app.ai.rag.embedder.settings.bge_m3_endpoint", "http://bge-m3:8000"), \
         patch("httpx.AsyncClient.post", new=mock_post):
        emb = BGE3Embedder()
        out = await emb.embed(["第一份标书", "第二份标书"])
    assert len(out) == 2
    assert len(out[0]) == 1024
    assert out[0][0] == 1.0  # 批序保留：第一条文本 → 向量 1
    assert out[1][0] == 2.0
    assert "/embed" in str(mock_post.call_args[0][0])
    assert mock_post.call_args[1]["json"]["normalize"] is True


@pytest.mark.asyncio
async def test_embed_empty_input():
    """空输入 → 空列表（不发起请求）。"""
    with patch("app.ai.rag.embedder.settings.bge_m3_endpoint", "http://bge-m3:8000"):
        emb = BGE3Embedder()
        assert await emb.embed([]) == []


@pytest.mark.asyncio
async def test_embed_http_error_raises():
    """HTTP 5xx → raise_for_status 抛 httpx.HTTPStatusError（调用方降级）。"""
    mock_post = AsyncMock(return_value=httpx.Response(
        503, json={}, request=httpx.Request("POST", "http://x"),
    ))
    with patch("app.ai.rag.embedder.settings.bge_m3_endpoint", "http://bge-m3:8000"), \
         patch("httpx.AsyncClient.post", new=mock_post):
        emb = BGE3Embedder()
        with pytest.raises(httpx.HTTPStatusError):
            await emb.embed(["文本"])

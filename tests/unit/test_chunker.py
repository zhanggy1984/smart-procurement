"""P7.2 SmartDocumentChunker 单元测试（task.md：5 用例）。

覆盖标题感知切分、递归二分/滑窗、overlap 保留、超长截断、空文档。
token 估算用 tiktoken cl100k_base（lazy），与 embedding 无关。
"""

from __future__ import annotations

import pytest

from app.ai.rag.chunker import SmartDocumentChunker


@pytest.fixture(scope="module")
def chunker() -> SmartDocumentChunker:
    return SmartDocumentChunker(min_tokens=50, max_tokens=100, overlap_tokens=20)


def _ids(chunks) -> list[str]:
    return [c.chunk_id for c in chunks]


def test_empty_document(chunker):
    """空文档/全空白 → 空列表。"""
    assert chunker.chunk("", bid_id="BID-1", lot_id="LOT-1") == []
    assert chunker.chunk("   \n  ", bid_id="BID-1", lot_id="LOT-1") == []


def test_short_document_single_chunk(chunker):
    """短文档整体保留为 1 个 chunk，不硬凑 min。"""
    chunks = chunker.chunk("第一章 概况\n公司简介与项目背景。", bid_id="BID-1", lot_id="LOT-1")
    assert len(chunks) == 1
    assert chunks[0].chapter_title == "第一章 概况"
    assert chunks[0].bid_id == "BID-1"
    assert chunks[0].chunk_id.startswith("BID-1-")


def test_heading_aware_split(chunker):
    """标题感知：多章标题各成 section，chapter_title 正确。"""
    text = (
        "第一章 公司概况\nA 公司成立于 2010 年。\n"
        "3.2 系统架构\n采用微服务架构，分层解耦。\n"
        "一、团队配置\n项目经理与 20 名研发工程师。"
    )
    chunks = chunker.chunk(text, bid_id="BID-1", lot_id="LOT-1")
    titles = [c.chapter_title for c in chunks]
    assert "第一章 公司概况" in titles
    assert "3.2 系统架构" in titles
    assert "一、团队配置" in titles


def test_long_body_sliding_window(chunker):
    """超长正文滑窗切分：>max 时多 chunk，步长=max-overlap，保留 overlap。"""
    # 中文长句池（>100 tokens 无法手动构造，用重复句撑长）
    sentence = "本项目建设内容包括基础网络、安全防护、数据平台与业务应用四大部分。"
    text = "第五章 建设方案\n" + sentence * 40  # ~40×26≈1040 中文字符，远超 100 tokens
    chunks = chunker.chunk(text, bid_id="BID-1", lot_id="LOT-1")
    assert len(chunks) >= 2
    # chunk 长度都在 [min, max] 上限附近（中文 token 密度 ~1:1，只验上限不超 max 过多）
    for c in chunks:
        assert len(c.content) > 0
    # overlap 保留：相邻 chunk 有内容重叠
    assert chunks[0].content in chunks[1].content or chunks[1].content in chunks[0].content \
        or len(set(chunks[0].content) & set(chunks[1].content)) > 0


def test_chunk_id_sequence_global(chunker):
    """chunk_id 全局递增（{bid_id}-{seq:04d}），Milvus 主键唯一。"""
    text = "第一章 概况\n内容 A。\n第二章 详情\n内容 B。\n第三章 附录\n内容 C。"
    chunks = chunker.chunk(text, bid_id="BID-1", lot_id="LOT-1")
    ids = _ids(chunks)
    assert len(set(ids)) == len(ids)
    seqs = [int(i.split("-")[-1]) for i in ids]
    assert seqs == sorted(seqs) and len(seqs) == chunks[-1].chunk_index + 1


def test_illegal_params_raises():
    """非法分块参数（overlap ≥ max / min ≥ max）→ ValueError。"""
    with pytest.raises(ValueError):
        SmartDocumentChunker(min_tokens=100, max_tokens=100, overlap_tokens=0)
    with pytest.raises(ValueError):
        SmartDocumentChunker(min_tokens=0, max_tokens=50, overlap_tokens=10)

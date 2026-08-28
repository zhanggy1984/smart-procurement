"""P7.2 RRF 融合排序单元测试（task.md：3 用例）+ 关键词打分。

覆盖 _rrf_fuse 两路融合正确性、k 参数、同分去重（rank 稳定）；
_score_keywords 词窗命中。
"""

from __future__ import annotations

import pytest

from app.ai.rag.retriever import (
    RetrievalResult,
    _confidence_band,
    _rrf_fuse,
    _score_keywords,
)


def test_rrf_fuse_two_routes():
    """两路召回 RRF 融合：rank 越靠前分越高，交集项出现在不同路的分被叠加。"""
    route_a = [("c1", 0.9), ("c2", 0.8), ("c3", 0.7)]
    route_b = [("c2", 0.95), ("c3", 0.6), ("c4", 0.5)]
    fused = _rrf_fuse({"vector": route_a, "keyword": route_b}, k=60, top_n=4)
    # 按 RRF：c2 在路 A rank2 + 路 B rank1 → 60+60/1 + 60/2 = 120+30=150？ 实为 1/(60+rank)
    # c2 两路均命中，得分最高 → 排第一
    assert fused[0][0] == "c2"
    # c4 只在路 B → 最后
    assert fused[-1][0] == "c4"
    # 全部去重（c2/c3 各只出现一次）
    ids = [f[0] for f in fused]
    assert len(ids) == len(set(ids))


def test_rrf_fuse_k_parameter():
    """k 影响权重：k 越大，rank 差的惩罚越小（小 k 更看重 rank 靠前）。"""
    route_a = [("c1", 1.0), ("c2", 0.5)]
    k_small = _rrf_fuse({"a": route_a}, k=1, top_n=2)
    k_large = _rrf_fuse({"a": route_a}, k=1000, top_n=2)
    # 两种 k 下排名都保持 route_a 顺序（单路不改变相对序）
    assert [x[0] for x in k_small] == ["c1", "c2"]
    assert [x[0] for x in k_large] == ["c1", "c2"]
    # 大 k 时 rank1 与 rank2 的分数差更小
    gap_small = k_small[0][1] - k_small[1][1]
    gap_large = k_large[0][1] - k_large[1][1]
    assert gap_large < gap_small


def test_rrf_fuse_top_n_and_empty():
    """top_n 截断 + 空输入兜底。"""
    route_a = [("c1", 0.9), ("c2", 0.8), ("c3", 0.7)]
    fused = _rrf_fuse({"a": route_a}, top_n=2)
    assert len(fused) == 2
    assert _rrf_fuse({}, top_n=8) == []
    assert _rrf_fuse({"a": []}, top_n=8) == []


def test_score_keywords_terms_hit():
    """关键词打分：含术语的 chunk 得正分，不含为 0。"""
    chunks = [
        {"chunk_id": "c1", "content": "系统架构采用微服务，架构分层清晰"},
        {"chunk_id": "c2", "content": "公司成立于 2010 年"},
    ]
    scored = _score_keywords(chunks, ["架构", "微服务"])
    by_id = {cid: s for cid, s in scored}
    assert by_id["c1"] > 0
    assert "c2" not in by_id  # 无命中 → 不出现在稀疏得分列表（等价 0 分）


# ==================== P7.x 检索置信档 + return_meta（function calling 契约标准化） ====================


def test_confidence_band_boundaries():
    """置信档三档边界：<0.5 none｜[0.5,0.65) low｜>=0.65 high；None 判无关。"""
    assert _confidence_band(None) == "none"
    assert _confidence_band(0.49) == "none"
    assert _confidence_band(0.5) == "low"
    assert _confidence_band(0.64) == "low"
    assert _confidence_band(0.65) == "high"
    assert _confidence_band(0.9) == "high"


@pytest.mark.asyncio
async def test_retrieve_with_meta_default_two_tuple(monkeypatch):
    """默认 return_meta=False 返回二元组（旧调用点零改动，accept_p24 依赖）。"""
    from app.ai.rag import retriever as R

    async def fake_internal(query, **kwargs):
        return [], 0.9, True

    monkeypatch.setattr(R, "_retrieve_internal", fake_internal)
    results, hint = await R.retrieve_with_meta("q", lot_id="L", bid_id="B")
    assert isinstance(results, list)
    assert hint is None


@pytest.mark.asyncio
async def test_retrieve_with_meta_return_meta(monkeypatch):
    """return_meta=True 返回三元组，meta 含 source_count/max_score/semantic_ok/confidence_band。"""
    from app.ai.rag import retriever as R

    result = RetrievalResult(
        chunk_id="c1", bid_id="B", lot_id="L", content="内容",
        chapter_title="章", page_range=[1, 1], score=0.5, source="vector",
    )

    async def fake_internal(query, **kwargs):
        return [result], 0.55, True

    monkeypatch.setattr(R, "_retrieve_internal", fake_internal)
    results, hint, meta = await R.retrieve_with_meta(
        "q", lot_id="L", bid_id="B", return_meta=True
    )
    assert len(results) == 1
    assert hint is None
    assert meta["source_count"] == 1
    assert meta["max_score"] == 0.55
    assert meta["semantic_ok"] is True
    assert meta["confidence_band"] == "low"  # 0.55 ∈ [0.5, 0.65)


@pytest.mark.asyncio
async def test_retrieve_with_meta_return_meta_low_score(monkeypatch):
    """低分（低于拒答阈值）→ hint=NO_EVIDENCE、confidence=none（评分 prompt 触发低置信段）。"""
    from app.ai.rag import retriever as R
    from app.ai.rag.degradation import DegradationHint

    async def fake_internal(query, **kwargs):
        return [], 0.3, True

    monkeypatch.setattr(R, "_retrieve_internal", fake_internal)
    results, hint, meta = await R.retrieve_with_meta(
        "q", lot_id="L", bid_id="B", return_meta=True
    )
    assert hint == DegradationHint.NO_EVIDENCE
    assert meta["source_count"] == 0
    assert meta["confidence_band"] == "none"


# ==================== P8 异常兜底：检索链路降级断裂 ====================


class _FakeEmbedder:
    """embed 可配（正常返回向量 / 抛异常模拟 BGE-M3 挂）。"""

    def __init__(self, raise_error: bool = False):
        self.raise_error = raise_error

    async def embed(self, texts):
        if self.raise_error:
            raise RuntimeError("embedding service down")
        return [[0.1, 0.2, 0.3]]


def _fake_chunks(lot_id, bid_id):
    return [
        {"chunk_id": "c1", "content": "系统采用微服务架构，分层清晰", "chapter_title": "章", "page_range": "1"},
    ]


async def _empty_structured(q, bid_id):
    """结构化路 mock：返回空（async，与 _structured_match 签名对齐）。"""
    return []


@pytest.mark.asyncio
async def test_embedding_failure_degrades_semantic(monkeypatch):
    """BGE-M3 挂 → 不抛异常，semantic_ok=False，关键词路仍出结果。"""
    from app.ai.rag import retriever as R

    monkeypatch.setattr(R, "get_embedder", lambda: _FakeEmbedder(raise_error=True))
    monkeypatch.setattr(R, "_query_all_chunks", _fake_chunks)
    monkeypatch.setattr(R, "_structured_match", _empty_structured)
    results, max_score, semantic_ok = await R._retrieve_internal(
        "微服务架构", lot_id="L", bid_id="B"
    )
    assert semantic_ok is False
    assert max_score is None
    assert len(results) > 0  # 关键词路兜底出结果
    assert all(r.source != "vector" for r in results)


@pytest.mark.asyncio
async def test_milvus_search_non_timeout_failure_degrades(monkeypatch):
    """Milvus search 非超时故障（连接/gRPC）→ semantic_ok=False，关键词结果仍在。"""
    from app.ai.rag import retriever as R

    monkeypatch.setattr(R, "get_embedder", lambda: _FakeEmbedder())

    def _raise(*a, **kw):
        raise RuntimeError("milvus conn refused")

    monkeypatch.setattr(R, "_search_vector", _raise)
    monkeypatch.setattr(R, "_query_all_chunks", _fake_chunks)
    monkeypatch.setattr(R, "_structured_match", _empty_structured)
    results, max_score, semantic_ok = await R._retrieve_internal(
        "微服务架构", lot_id="L", bid_id="B"
    )
    assert semantic_ok is False
    assert max_score is None
    assert len(results) > 0  # 关键词路仍可用


@pytest.mark.asyncio
async def test_query_all_chunks_failure_degrades_to_structured(monkeypatch):
    """Milvus 整体不可用（search + query 均挂）→ SEMANTIC_DOWN + 结构化路兜底。"""
    from app.ai.rag import retriever as R
    from app.ai.rag.degradation import DegradationHint

    monkeypatch.setattr(R, "get_embedder", lambda: _FakeEmbedder())

    def _raise(*a, **kw):
        raise RuntimeError("milvus conn refused")

    monkeypatch.setattr(R, "_search_vector", _raise)
    monkeypatch.setattr(R, "_query_all_chunks", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("milvus query down")))

    async def _one_structured(q, bid_id):
        return [("structured:B:质量", 1.0)]

    monkeypatch.setattr(R, "_structured_match", _one_structured)
    results, hint = await R.retrieve_with_meta("质量", lot_id="L", bid_id="B")
    assert hint == DegradationHint.SEMANTIC_DOWN
    assert len(results) == 1
    assert results[0].source == "structured"  # 结构化路兜底


@pytest.mark.asyncio
async def test_all_chunks_down_keeps_vector_hits(monkeypatch):
    """Milvus query（全量拉取）挂，但向量检索成功 → 向量命中保留（自查 #5）。

    原实现：all_chunks 失败 → chunk_info={} → 向量命中元数据缺失被整体丢弃，
    语义检索明明成功却空手而归；且 query 失败还把 semantic_ok 置 False 污染置信信号。
    """
    from app.ai.rag import retriever as R

    monkeypatch.setattr(R, "get_embedder", lambda: _FakeEmbedder())
    monkeypatch.setattr(
        R, "_search_vector",
        lambda *a, **kw: [(
            "c1", 0.85,
            {"chunk_id": "c1", "content": "系统采用微服务架构，分层清晰",
             "chapter_title": "技术方案", "page_range": "3",
             "heading_level": 1, "source_type": "paragraph", "token_count": 12},
        )],
    )
    monkeypatch.setattr(R, "_query_all_chunks", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("milvus query down")))
    monkeypatch.setattr(R, "_structured_match", _empty_structured)
    results, max_score, semantic_ok = await R._retrieve_internal(
        "微服务架构", lot_id="L", bid_id="B"
    )
    # 向量命中不丢，且不被 query 失败降级
    assert semantic_ok is True
    assert max_score == 0.85
    assert len(results) == 1
    assert results[0].source == "vector"
    assert results[0].content == "系统采用微服务架构，分层清晰"
    assert results[0].chapter_title == "技术方案"
    assert results[0].page_range == [3, 3]

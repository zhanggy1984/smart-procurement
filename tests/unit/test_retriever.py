"""P7.2 RRF 融合排序单元测试（task.md：3 用例）+ 关键词打分。

覆盖 _rrf_fuse 两路融合正确性、k 参数、同分去重（rank 稳定）；
_score_keywords 词窗命中。
"""

from __future__ import annotations

from app.ai.rag.retriever import _rrf_fuse, _score_keywords


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

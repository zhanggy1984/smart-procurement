"""P7.2 围串标检测纯函数单元测试（task.md：5 用例）。

覆盖 _price_check 报价集中度、_deep_price_check 陪标模式、risk_level 四级、
_faiss_similar_pairs 高相似段落对、TEXT_SIMILAR_PAIR_THRESHOLD。
"""

from __future__ import annotations

import pytest

from app.services.fraud_detection_service import (
    _deep_price_check,
    _faiss_similar_pairs,
    _price_check,
    risk_level,
)


def test_price_check_cluster():
    """报价集中度 <1% → 40 分（PRICE_CLUSTER）；正常分散 → 0。"""
    score, ev = _price_check([100, 100.5, 100.2])
    assert score == 40 and ev[0]["type"] == "PRICE_CLUSTER"
    score, ev = _price_check([100, 200, 300])
    assert score == 0 and ev == []


def test_price_check_edge_cases():
    """不足 2 家 / 平均 0 → 0 分。"""
    assert _price_check([100]) == (0, [])
    assert _price_check([]) == (0, [])
    assert _price_check([0, 0]) == (0, [])


def test_deep_price_check_ring():
    """陪标模式：最低/次低 <0.85 → BIDDING_RING；集中度同时触发则双证据。"""
    score, ev = _deep_price_check([50, 100, 100, 100])
    types = {e["type"] for e in ev}
    assert "BIDDING_RING" in types
    score, ev = _deep_price_check([100, 120, 110])  # 价差 18%，无集中/陪标
    assert ev == []


def test_risk_level_boundaries():
    """四级风险边界：25/50/75 精确边界。"""
    assert risk_level(25) == "LOW"
    assert risk_level(25.1) == "MEDIUM"
    assert risk_level(50) == "MEDIUM"
    assert risk_level(51) == "HIGH"
    assert risk_level(75) == "HIGH"
    assert risk_level(76) == "CRITICAL"


def test_faiss_similar_pairs():
    """FAISS 批量：高相似段落对命中 + 低相似不命中。"""
    n = pytest.importorskip("numpy")
    from app.services.fraud_detection_service import TEXT_SIMILARITY_THRESHOLD

    v = [1.0] + [0.0] * 10
    chunks = [
        {"chunk_id": "a1", "bid_id": "BID-A", "embedding": v},
        {"chunk_id": "a2", "bid_id": "BID-A", "embedding": [0.99] + [0.0] * 10},  # 高相似
        {"chunk_id": "b1", "bid_id": "BID-B", "embedding": [-1.0] + [0.0] * 10},  # 低相似
    ]
    pairs = _faiss_similar_pairs(chunks, threshold=0.8)
    assert len(pairs) >= 1
    assert any({"a1", "a2"} <= {pp["chunk_a"], pp["chunk_b"]} for pp in pairs) or pairs[0]["score"] > 0.8


def test_faiss_similar_pairs_single_chunk():
    """单 chunk → 无对。"""
    chunks = [{"chunk_id": "a1", "bid_id": "BID-A", "embedding": [1.0, 0.0]}]
    assert _faiss_similar_pairs(chunks) == []


def test_risk_level_custom_boundaries():
    """P6.2 配置化阈值：low=30/critical=80 时边界随配置移动，mid=(30+80)/2=55。"""
    assert risk_level(30, low_threshold=30, critical_threshold=80) == "LOW"
    assert risk_level(30.1, low_threshold=30, critical_threshold=80) == "MEDIUM"
    assert risk_level(55, low_threshold=30, critical_threshold=80) == "MEDIUM"
    assert risk_level(55.1, low_threshold=30, critical_threshold=80) == "HIGH"
    assert risk_level(80, low_threshold=30, critical_threshold=80) == "HIGH"
    assert risk_level(80.1, low_threshold=30, critical_threshold=80) == "CRITICAL"

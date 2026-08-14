"""P7.2 降级判定单元测试（task.md 降级路径：classify_retrieval 全分支）。

覆盖：语义不可用 → SEMANTIC_DOWN；无结果未解析 → PARSING；
全低分 → NO_EVIDENCE；相关 → None（正常）。
"""

from __future__ import annotations

from app.ai.rag.degradation import (
    DegradationHint,
    SIMILARITY_THRESHOLD,
    classify_retrieval,
)


def test_semantic_down_on_timeout():
    """Milvus 超时（semantic_ok=False）→ SEMANTIC_DOWN，优先于其他分支。"""
    assert classify_retrieval(0.9, bid_parsed=True, semantic_ok=False) == DegradationHint.SEMANTIC_DOWN
    assert classify_retrieval(None, bid_parsed=False, semantic_ok=False) == DegradationHint.SEMANTIC_DOWN


def test_parsing_when_no_result_and_unparsed():
    """无结果 + 标书未解析 → PARSING。"""
    assert classify_retrieval(None, bid_parsed=False, semantic_ok=True) == DegradationHint.PARSING


def test_no_evidence_when_no_result_parsed():
    """无结果 + 已解析 → NO_EVIDENCE（拒答）。"""
    assert classify_retrieval(None, bid_parsed=True, semantic_ok=True) == DegradationHint.NO_EVIDENCE


def test_no_evidence_below_threshold():
    """全部 chunk 低于 IP<0.5 → NO_EVIDENCE（拒答）。"""
    assert classify_retrieval(SIMILARITY_THRESHOLD - 0.01, bid_parsed=True, semantic_ok=True) == DegradationHint.NO_EVIDENCE


def test_normal_above_threshold():
    """最高 IP ≥0.5 且语义正常 → None（正常评分）。"""
    assert classify_retrieval(SIMILARITY_THRESHOLD, bid_parsed=True, semantic_ok=True) is None
    assert classify_retrieval(0.8, bid_parsed=True, semantic_ok=True) is None

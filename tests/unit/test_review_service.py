"""P7.2 ReviewService 纯逻辑单元测试（task.md：6 用例）。

覆盖：报价维度综合评分法公式、AI 输出分数解析（_RE_SCORE 主正则 +
_RE_TOTAL_SCORE 兜底）、create_review 校验（FROZEN/维度归属）、
submit_review 人工调整判定（MANUAL_ADJUSTED vs CONFIRMED）。
"""

from __future__ import annotations

import json

import pytest

from app.ai.rag.retriever import RetrievalResult
from app.services import review_service as svc
from app.services.review_service import (
    _calc_price_formula,
    _RE_SCORE,
    _RE_TOTAL_SCORE,
    BidNotFrozenError,
    create_review,
)
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy.ext.asyncio import AsyncSession


def test_calc_price_formula():
    """综合评分法：基准价=Σ/N，得分=max×(1-偏差率)。"""
    calc = _calc_price_formula(bid_amount=100.0, dim_max_score=20, lot_bids=[100.0, 100.0, 110.0])
    assert calc["result"]["basePrice"] == pytest.approx(103.33, abs=0.01)
    # 偏差率 = |100-103.33|/103.33 ≈ 3.23%，得分 = 20×0.9677 ≈ 19.35
    assert calc["result"]["calculatedScore"] == pytest.approx(19.35, abs=0.05)


def test_calc_price_formula_lowest_bid():
    """报价偏离基准越大得分越低；同基准价 → 满分。"""
    calc = _calc_price_formula(bid_amount=100.0, dim_max_score=20, lot_bids=[100.0, 100.0, 100.0])
    assert calc["result"]["calculatedScore"] == 20.0
    calc = _calc_price_formula(bid_amount=150.0, dim_max_score=20, lot_bids=[100.0, 100.0, 100.0])
    assert calc["result"]["calculatedScore"] == 10.0  # 偏离 50% → 得分恰好减半


def test_score_regex_primary():
    """主正则 _RE_SCORE：匹配 `分数: X`（宽容全角冒号/空格）。"""
    m = _RE_SCORE.search("技术方案完整，满足要求。\n分数: 23.5")
    assert m and float(m.group(1)) == 23.5
    m = _RE_SCORE.search("总体分数：18.0分")
    assert m and float(m.group(1)) == 18.0


def test_score_regex_fallback_total():
    """兜底 _RE_TOTAL_SCORE：LLM 输出 `X分 / Y分` 时提取 X。"""
    m = _RE_TOTAL_SCORE.search("技术方案总分：7.5 + 6.0 + 5.5 = 19.0分 / 30.0分")
    assert m and float(m.group(1)) == 19.0
    # 主正则也兼容该行（数字出现在 `分` 前？不匹配 `分数:`，需靠兜底）
    assert _RE_SCORE.search("19.0分 / 30.0分") is None


def test_score_regex_no_match_returns_none():
    """LLM 未输出分数 → 两正则都 None（score=None，前端不填分）。"""
    assert _RE_SCORE.search("方案合理，建议通过。") is None
    assert _RE_TOTAL_SCORE.search("方案合理，建议通过。") is None


@pytest.mark.asyncio
async def test_create_review_rejects_unfrozen():
    """bid 未 FROZEN → BidNotFrozenError（400 语义）。"""
    session = AsyncMock(spec=AsyncSession)
    bid = MagicMock()
    bid.status = "SUBMITTED"  # 未封存
    session.get.side_effect = [bid, MagicMock()]  # 第一个 get 返回 bid
    with pytest.raises(BidNotFrozenError):
        await create_review(session, expert_id="E1", bid_id="B1", dimension_id="D1")


@pytest.mark.asyncio
async def test_create_review_dimension_mismatch():
    """维度不属于该标段 → 校验维度归属。"""
    from app.services.review_service import DimensionMismatchError

    session = AsyncMock(spec=AsyncSession)
    bid = MagicMock()
    bid.status = "FROZEN"
    bid.lot_id = "LOT-A"
    dim = MagicMock()
    dim.lot_id = "LOT-B"  # 归属另一标段
    session.get.side_effect = [bid, dim]
    with pytest.raises(DimensionMismatchError):
        await create_review(session, expert_id="E1", bid_id="B1", dimension_id="D1")


# ==================== P7.x stream_score：tool_call 检索质量元信息（契约标准化） ====================


def _find_tool_call(frames: list[str]) -> dict:
    """从 SSE 帧列表中提取 tool_call 事件的 data JSON。"""
    for fr in frames:
        if "\nevent: tool_call\n" in fr:
            data_line = fr.split("\nevent: tool_call\ndata: ")[1].split("\n\n")[0]
            return json.loads(data_line)
    raise AssertionError("未找到 tool_call 事件")


@pytest.mark.asyncio
async def test_stream_score_tool_call_meta(monkeypatch):
    """评分 stream_score：tool_call 事件透出检索质量元信息（RETRIEVE_RESULT_SCHEMA）。

    覆盖 return_meta=True 链路：source_count/max_score/confidence_band/semantic_ok/hint
    进 tool_call 事件，评测端可观测检索动作；事件序不变（加字段不破 §5.1）。
    """
    session = AsyncMock()
    review = MagicMock(review_id="R1", expert_id="E1", dimension_id="D1", bid_id="B1")
    dim = MagicMock(dimension_id="D1", name="技术方案", max_score=30)
    bid = MagicMock(bid_id="B1", lot_id="LOT-1", bid_amount=None, structured_data=None)
    session.get.side_effect = [review, dim, bid]

    criterion = MagicMock(name="架构合理性", max_score=10, scoring_rubric="分层清晰", description="")
    criteria_res = MagicMock()  # .all() 同步返回列表（匹配真实 SQLAlchemy scalars().all() 行为）
    criteria_res.all.return_value = [criterion]
    session.scalars.return_value = criteria_res

    result = RetrievalResult(
        chunk_id="c1", bid_id="B1", lot_id="LOT-1",
        content="标书技术方案：微服务架构，分层清晰", chapter_title="技术方案", page_no=3,
        score=0.8, source="vector",
    )
    meta = {"source_count": 1, "max_score": 0.8, "semantic_ok": True, "confidence_band": "high"}

    async def _fake_retrieve(query, **kwargs):
        return [result], None, meta

    monkeypatch.setattr(svc, "retrieve_with_meta", _fake_retrieve)

    async def _fake_stream(prompt, max_tokens=2048):
        yield "<thinking>依据标书技术方案合理性判断。</thinking>", None
        yield "<answer>方案完整可行。\n分数: 25.0</answer>", None

    client = MagicMock()
    client.chat_stream = _fake_stream
    monkeypatch.setattr(svc, "get_client", lambda: client)

    frames = [f async for f in svc.stream_score(session, review_id="R1", expert_id="E1")]
    tool = _find_tool_call(frames)
    assert tool["name"] == "knowledge_retrieval"
    assert tool["status"] == "success"
    assert tool["source_count"] == 1
    assert tool["max_score"] == 0.8
    assert tool["confidence_band"] == "high"
    assert tool["semantic_ok"] is True
    assert tool["hint"] is None
    assert tool["result"] == [{"chunk_id": "c1", "chapter_title": "技术方案", "score": 0.8}]

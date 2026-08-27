"""P7.2 ReviewService 流程补深（task.md：评审保存/提交/流式评分）。

覆盖（演示链路场景1 关键）：
- create_review：FROZEN + 维度归属校验成功路径
- save_score：DRAFT 暂存成功；已提交锁定 → ReviewLockedError；非本人 → 拒绝
- submit_review：采纳 AI 建议 → CONFIRMED；手动调整 → MANUAL_ADJUSTED；重复提交幂等
- stream_score 报价维度：纯公式 SSE 事件序 thinking → price_calc → done（不走 AI）
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.bid_document import BidStatus
from app.models.expert_review import ReviewStatus
from app.services.review_service import (
    PRICE_DIMENSION_NAME,
    BidNotFrozenError,
    ReviewAccessDeniedError,
    ReviewLockedError,
    ReviewNotFoundError,
    create_review,
    get_lot_review_progress,
    list_my_reviews,
    save_score,
    stream_score,
    submit_review,
)


@pytest.mark.asyncio
async def test_create_review_success():
    """bid=FROZEN + 维度归属匹配 + 专家已分配 → 创建 DRAFT 评审工作台。"""
    session = AsyncMock()
    bid = MagicMock()
    bid.status = BidStatus.FROZEN
    bid.lot_id = "LOT-A"
    dim = MagicMock()
    dim.lot_id = "LOT-A"
    assignment = MagicMock()
    assignment.dimension_ids = ["D-1"]
    session.get.side_effect = [bid, dim]
    session.scalar.return_value = assignment
    review = await create_review(session, expert_id="EXP-1", bid_id="BID-1", dimension_id="D-1")
    assert review.status == "DRAFT"
    session.add.assert_called_once()
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_review_expert_not_assigned_403():
    """专家未被分配标段 → 越权拒绝（评审水平越权防护）。"""
    session = AsyncMock()
    bid = MagicMock()
    bid.status = BidStatus.FROZEN
    bid.lot_id = "LOT-A"
    dim = MagicMock()
    dim.lot_id = "LOT-A"
    session.get.side_effect = [bid, dim]
    session.scalar.return_value = None
    with pytest.raises(ReviewAccessDeniedError):
        await create_review(session, expert_id="EXP-9", bid_id="BID-1", dimension_id="D-1")


@pytest.mark.asyncio
async def test_create_review_dimension_not_in_assignment_403():
    """专家已分配标段但维度不在负责维度 → 越权拒绝。"""
    session = AsyncMock()
    bid = MagicMock()
    bid.status = BidStatus.FROZEN
    bid.lot_id = "LOT-A"
    dim = MagicMock()
    dim.lot_id = "LOT-A"
    assignment = MagicMock()
    assignment.dimension_ids = ["D-OTHER"]
    session.get.side_effect = [bid, dim]
    session.scalar.return_value = assignment
    with pytest.raises(ReviewAccessDeniedError):
        await create_review(session, expert_id="EXP-1", bid_id="BID-1", dimension_id="D-1")


@pytest.mark.asyncio
async def test_save_score_success():
    """DRAFT 暂存：score/comment/ai_suggestion 写入并 commit。"""
    session = AsyncMock()
    review = MagicMock()
    review.expert_id = "EXP-1"
    review.status = "DRAFT"
    review.score = None
    session.get.return_value = review
    saved = await save_score(session, review_id="REV-1", expert_id="EXP-1",
                             score=19.5, comment="方案可行", ai_suggestion={"score": 19.5})
    assert saved is review
    assert review.score == Decimal("19.5")
    assert review.comment == "方案可行"
    assert review.status == "DRAFT"
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_save_score_locked_rejected():
    """已提交锁定（CONFIRMED）→ ReviewLockedError，不可回改。"""
    session = AsyncMock()
    review = MagicMock()
    review.expert_id = "EXP-1"
    review.status = "CONFIRMED"
    session.get.return_value = review
    with pytest.raises(ReviewLockedError):
        await save_score(session, review_id="REV-1", expert_id="EXP-1", score=1.0, comment="改")
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_save_score_not_own():
    """非本人评审 → ReviewAccessDeniedError。"""
    session = AsyncMock()
    review = MagicMock()
    review.expert_id = "EXP-2"
    session.get.return_value = review
    with pytest.raises(ReviewAccessDeniedError):
        await save_score(session, review_id="REV-1", expert_id="EXP-1", score=1.0, comment="x")


@pytest.mark.asyncio
async def test_save_score_not_found():
    """评审记录不存在 → ReviewNotFoundError。"""
    session = AsyncMock()
    session.get.return_value = None
    with pytest.raises(ReviewNotFoundError):
        await save_score(session, review_id="REV-X", expert_id="EXP-1", score=1.0, comment="x")


@pytest.mark.asyncio
async def test_submit_review_confirmed():
    """score 与 AI 建议一致 → CONFIRMED（采纳 AI 建议）。"""
    session = AsyncMock()
    review = MagicMock()
    review.expert_id = "EXP-1"
    review.status = "DRAFT"
    review.score = Decimal("19.5")
    review.ai_suggestion = {"score": 19.5}
    session.get.return_value = review
    done = await submit_review(session, review_id="REV-1", expert_id="EXP-1")
    assert done.status == "CONFIRMED"
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_submit_review_manual_adjusted():
    """score 与 AI 建议偏差 >0.01 → MANUAL_ADJUSTED（人工调整）。"""
    session = AsyncMock()
    review = MagicMock()
    review.expert_id = "EXP-1"
    review.status = "DRAFT"
    review.score = Decimal("10.0")
    review.ai_suggestion = {"score": 18.0}  # 人工改为 10，与建议不符
    session.get.return_value = review
    done = await submit_review(session, review_id="REV-1", expert_id="EXP-1")
    assert done.status == "MANUAL_ADJUSTED"


@pytest.mark.asyncio
async def test_submit_review_idempotent():
    """已提交（CONFIRMED）重复提交 → 幂等返回，不再 commit。"""
    session = AsyncMock()
    review = MagicMock()
    review.expert_id = "EXP-1"
    review.status = "CONFIRMED"
    session.get.return_value = review
    done = await submit_review(session, review_id="REV-1", expert_id="EXP-1")
    assert done is review
    session.commit.assert_not_awaited()


def _parse_sse(frame: str) -> dict:
    """解析 SSE 帧 `id: N\\nevent: X\\ndata: {json}\\n\\n` → dict。"""
    d: dict = {}
    for line in frame.split("\n"):
        if line.startswith("id: "):
            d["id"] = int(line[4:])
        elif line.startswith("event: "):
            d["event"] = line[7:]
        elif line.startswith("data: "):
            d["data"] = json.loads(line[6:])
    return d


async def _collect_stream(session, review_id, expert_id):
    return [_parse_sse(ev) async for ev in stream_score(session, review_id=review_id, expert_id=expert_id)]


@pytest.mark.asyncio
async def test_stream_score_price_formula_events():
    """报价维度 SSE：meta 首帧 → thinking(PRICE_CALC) → price_calc → done，事件序与 seq 递增。"""
    session = AsyncMock()
    review = MagicMock()
    review.expert_id = "EXP-1"
    review.review_id = "REV-1"
    review.dimension_id = "D-P"
    review.bid_id = "BID-1"
    dim = MagicMock()
    dim.name = PRICE_DIMENSION_NAME
    dim.max_score = 20
    bid = MagicMock()
    bid.lot_id = "LOT-1"
    bid.bid_amount = 100
    bid.status = BidStatus.FROZEN
    session.get.side_effect = [review, dim, bid]

    lot_bids = MagicMock()
    lot_bids.all.return_value = [Decimal("100"), Decimal("120"), Decimal("80")]
    session.scalars.return_value = lot_bids

    events = await _collect_stream(session, "REV-1", "EXP-1")
    # 契约 meta 首帧（§5.1）：每个 SSE 流统一透出 agent 元信息
    assert [ev["event"] for ev in events] == ["meta", "thinking", "price_calc", "done"]
    assert events[0]["event"] == "meta"
    assert events[0]["id"] == 1
    # thinking(PRICE_CALC) 紧随 meta，seq=2
    assert events[1]["id"] == 2
    assert events[1]["data"]["stage"] == "PRICE_CALC"
    # 公式可审计：基准价 100，报价 100 → 满分 20
    assert events[2]["data"]["result"]["calculatedScore"] == 20.0
    assert events[2]["data"]["result"]["basePrice"] == 100.0
    # seq 递增（P3.6 断流续推用 Last-Event-ID）
    seqs = [ev["id"] for ev in events]
    assert seqs == [1, 2, 3, 4]
    # 公式可审计：基准价 = Σ/N = (100+120+80)/3 = 100


@pytest.mark.asyncio
async def test_stream_score_not_own():
    """非本人 → ReviewAccessDeniedError。"""
    session = AsyncMock()
    review = MagicMock()
    review.expert_id = "EXP-2"
    session.get.return_value = review
    with pytest.raises(ReviewAccessDeniedError):
        await _collect_stream(session, "REV-1", "EXP-1")


@pytest.mark.asyncio
async def test_stream_score_not_found():
    """评审不存在 → ReviewNotFoundError。"""
    session = AsyncMock()
    session.get.return_value = None
    with pytest.raises(ReviewNotFoundError):
        await _collect_stream(session, "REV-X", "EXP-1")


@pytest.mark.asyncio
async def test_list_my_reviews_joins_meta():
    """评审历史：关联供应商/标段/维度名，仅统计已提交。"""
    session = AsyncMock()
    rev = MagicMock()
    rev.bid_id = "BID-1"
    rev.dimension_id = "D-T"
    rev.status = ReviewStatus.CONFIRMED
    rev.score = Decimal("19.5")
    rev.updated_at = datetime(2026, 8, 12, tzinfo=timezone.utc)
    from types import SimpleNamespace

    total_result = MagicMock()
    total_result.all.return_value = [rev]
    page_result = MagicMock()
    page_result.all.return_value = [rev]
    lot_result = MagicMock()
    lot_result.all.return_value = [SimpleNamespace(lot_id="LOT-1", name="一标段")]
    dim_result = MagicMock()
    dim_result.all.return_value = [SimpleNamespace(dimension_id="D-T", name="技术", max_score=30)]
    # scalars：total → page → lots → dims
    session.scalars.side_effect = [total_result, page_result, lot_result, dim_result]
    bid_rows = MagicMock()
    bid_rows.all.return_value = [(SimpleNamespace(bid_id="BID-1", lot_id="LOT-1"),
                                  SimpleNamespace(name="供应商甲"))]
    session.execute.return_value = bid_rows

    items, total = await list_my_reviews(session, expert_id="EXP-1")
    assert total == 1
    assert items[0]["supplier_name"] == "供应商甲"
    assert items[0]["lot_name"] == "一标段"
    assert items[0]["dimension_name"] == "技术"
    assert items[0]["score"] == 19.5
    assert items[0]["status"] == ReviewStatus.CONFIRMED


@pytest.mark.asyncio
async def test_get_lot_review_progress_matrix():
    """评审进度矩阵：CONFIRMED 计入 done，percent 计算。"""
    session = AsyncMock()
    lot = MagicMock()
    lot.lot_id = "LOT-1"
    lot.lot_code = "LC-1"
    lot.name = "一标段"
    lot.status = "UNDER_REVIEW"
    lot.budget = 100000
    session.get.return_value = lot

    dims = [MagicMock(dimension_id="D-P", name="报价", max_score=20),
            MagicMock(dimension_id="D-T", name="技术", max_score=30)]
    dim_result = MagicMock()
    dim_result.all.return_value = dims
    rev = MagicMock()
    rev.bid_id = "BID-1"
    rev.dimension_id = "D-P"
    rev.status = ReviewStatus.CONFIRMED
    rev.score = Decimal("20")
    rev.expert_id = "EXP-1"
    rev.created_at = datetime(2026, 8, 12)
    from types import SimpleNamespace

    rev_result = MagicMock()
    rev_result.all.return_value = [rev]
    exp_result = MagicMock()
    exp_result.all.return_value = [SimpleNamespace(expert_id="EXP-1", name="张三")]
    session.scalars.side_effect = [dim_result, rev_result, exp_result]

    bid = MagicMock(bid_id="BID-1", supplier_id="S1", status="FROZEN")
    sup = MagicMock(supplier_id="S1", name="供应商甲")
    row_result = MagicMock()
    row_result.all.return_value = [(bid, sup)]
    session.execute.return_value = row_result

    res = await get_lot_review_progress(session, lot_id="LOT-1")
    assert res["progress"] == {"total": 2, "done": 1, "pending": 1, "percent": 50.0}
    # 已提交格带专家名
    cells = {c["dimension_id"]: c for b in res["bids"] for c in b["cells"]}
    assert cells["D-P"]["expert_name"] == "张三"
    assert cells["D-P"]["review_status"] == ReviewStatus.CONFIRMED
    assert cells["D-T"]["review_status"] is None  # 未分配

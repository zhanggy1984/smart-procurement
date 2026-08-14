"""P7.2 CloseoutService 单元测试（task.md：收尾流程）。

覆盖（演示链路场景1 定标关键）：
- complete_review：全部锁定 → EVALUATED + 报告；非 UNDER_REVIEW → 拒绝；
  存在 DRAFT 格子 → ReviewsIncompleteError（完整性校验）；lot 不存在 → 404
- get_lot_summary：综合得分归一化百分制、排名降序、同分报价低优先、状态 LOCKED/PENDING
- submit_for_award：项目下全部标段终态 → AWARDED + 触发归档 job；未完成 → 拒绝
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.bid_document import BidStatus
from app.models.expert_review import ReviewStatus
from app.services.closeout_service import (
    LotNotFoundError,
    LotNotUnderReviewError,
    ProjectNotReadyError,
    complete_review,
    get_lot_summary,
    submit_for_award,
)


@pytest.mark.asyncio
async def test_complete_review_success():
    """全部评审锁定（无 DRAFT）→ 生成报告 + lot=EVALUATED。"""
    session = AsyncMock()
    lot = MagicMock()
    lot.lot_id = "LOT-1"
    lot.status = "UNDER_REVIEW"
    session.get.return_value = lot

    bid = MagicMock()
    bid.bid_id = "BID-1"
    bid_result = MagicMock()
    bid_result.all.return_value = [bid]
    rev = MagicMock()
    rev.status = ReviewStatus.CONFIRMED
    rev_result = MagicMock()
    rev_result.all.return_value = [rev]
    session.scalars.side_effect = [bid_result, rev_result]

    with patch("app.services.closeout_service._build_report_pdf", new=AsyncMock(return_value=b"%PDF")):
        res = await complete_review(session, lot_id="LOT-1", operator_id="U-1")
    assert res["lot_id"] == "LOT-1"
    assert res["status"] == "EVALUATED"
    assert res["report_pdf"] == b"%PDF"
    assert lot.status == "EVALUATED"
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_complete_review_lot_not_found():
    """lot 不存在 → 404。"""
    session = AsyncMock()
    session.get.return_value = None
    with pytest.raises(LotNotFoundError):
        await complete_review(session, lot_id="LOT-X", operator_id="U-1")


@pytest.mark.asyncio
async def test_complete_review_not_under_review():
    """lot 非 UNDER_REVIEW → 400。"""
    session = AsyncMock()
    lot = MagicMock()
    lot.status = "PRE_SCREEN"  # 还在初筛待办，不可结束评审
    session.get.return_value = lot
    with pytest.raises(LotNotUnderReviewError):
        await complete_review(session, lot_id="LOT-1", operator_id="U-1")


@pytest.mark.asyncio
async def test_complete_review_blocks_draft():
    """存在 DRAFT 评审 → ReviewsIncompleteError（完整性校验，功能正确性关键）。"""
    session = AsyncMock()
    lot = MagicMock()
    lot.lot_id = "LOT-1"
    lot.status = "UNDER_REVIEW"
    session.get.return_value = lot

    bid = MagicMock()
    bid.bid_id = "BID-1"
    bid_result = MagicMock()
    bid_result.all.return_value = [bid]
    rev = MagicMock()
    rev.status = ReviewStatus.DRAFT  # 未完成格子
    rev_result = MagicMock()
    rev_result.all.return_value = [rev]
    session.scalars.side_effect = [bid_result, rev_result]

    with pytest.raises(Exception) as ei:
        await complete_review(session, lot_id="LOT-1", operator_id="U-1")
    assert "DRAFT" in str(ei.value)
    # 状态不得流转
    assert lot.status == "UNDER_REVIEW"


def _mk_lot(**kw):
    lot = MagicMock()
    lot.lot_id = kw.get("lot_id", "LOT-1")
    lot.lot_code = kw.get("lot_code", "LC-1")
    lot.name = kw.get("name", "测试标段")
    lot.status = kw.get("status", "UNDER_REVIEW")
    lot.budget = kw.get("budget", 1000000)
    lot.project_id = kw.get("project_id", "PRJ-1")
    return lot


@pytest.mark.asyncio
async def test_get_lot_summary_weighted_ranking():
    """综合得分 = 100×Σ(weight×score/max)，排名降序，LOCKED/PENDING 状态。"""
    session = AsyncMock()
    lot = _mk_lot()
    project = MagicMock()
    project.project_code = "PC-1"
    project.name = "测试项目"
    session.get.side_effect = [lot, project]

    d_price = MagicMock(dimension_id="D-P", name="报价", max_score=20, weight=0.4, sort_order=1)
    d_tech = MagicMock(dimension_id="D-T", name="技术", max_score=30, weight=0.6, sort_order=2)
    dim_result = MagicMock()
    dim_result.all.return_value = [d_price, d_tech]

    bid1 = MagicMock(bid_id="BID-1", supplier_id="S1", bid_amount=100, duration=90, team_size=10, status="FROZEN")
    sup1 = MagicMock(supplier_id="S1", name="供应商甲")
    bid2 = MagicMock(bid_id="BID-2", supplier_id="S2", bid_amount=120, duration=100, team_size=8, status="FROZEN")
    sup2 = MagicMock(supplier_id="S2", name="供应商乙")
    row_result = MagicMock()
    row_result.all.return_value = [(bid1, sup1), (bid2, sup2)]
    session.execute.return_value = row_result

    def _r(bid_id, dim_id, status, score):
        r = MagicMock()
        r.bid_id = bid_id
        r.dimension_id = dim_id
        r.status = status
        r.score = Decimal(str(score)) if score is not None else None
        return r

    reviews = [
        _r("BID-1", "D-P", ReviewStatus.CONFIRMED, 20),
        _r("BID-1", "D-T", ReviewStatus.CONFIRMED, 30),  # 满分 → 综合 100
        _r("BID-2", "D-P", ReviewStatus.CONFIRMED, 20),
        _r("BID-2", "D-T", ReviewStatus.MANUAL_ADJUSTED, 15),  # 0.4+0.3=0.7 → 70
        _r("BID-2", "D-T", ReviewStatus.DRAFT, 30),  # DRAFT 不计入
    ]
    rev_result = MagicMock()
    rev_result.all.return_value = reviews
    # scalars 第一次=维度，第二次=评审记录
    session.scalars.side_effect = [dim_result, rev_result]

    res = await get_lot_summary(session, lot_id="LOT-1")
    assert res["lot"]["project_code"] == "PC-1"
    assert res["lot"]["project_name"] == "测试项目"
    assert len(res["dimensions"]) == 2
    assert res["bids"][0]["bid_id"] == "BID-1"  # 综合 100 排第一
    assert res["bids"][0]["weighted_total"] == 100.0
    assert res["bids"][0]["rank"] == 1
    assert res["bids"][1]["weighted_total"] == 70.0
    # 维度状态：BID-1 全 LOCKED
    assert all(c["status"] == "LOCKED" for c in res["bids"][0]["dimension_scores"])


@pytest.mark.asyncio
async def test_get_lot_summary_tie_break_by_lower_price():
    """同综合得分 → 报价低者排前。"""
    session = AsyncMock()
    lot = _mk_lot()
    session.get.side_effect = [lot, MagicMock()]

    dim = MagicMock(dimension_id="D-P", name="报价", max_score=20, weight=1.0, sort_order=1)
    dim_result = MagicMock()
    dim_result.all.return_value = [dim]

    bid_x = MagicMock(bid_id="BID-X", supplier_id="SX", bid_amount=100, duration=1, team_size=1, status="FROZEN")
    bid_y = MagicMock(bid_id="BID-Y", supplier_id="SY", bid_amount=110, duration=1, team_size=1, status="FROZEN")
    row_result = MagicMock()
    row_result.all.return_value = [(bid_x, MagicMock(name="甲")), (bid_y, MagicMock(name="乙"))]
    session.execute.return_value = row_result

    def _r(bid_id, score):
        r = MagicMock()
        r.bid_id = bid_id
        r.dimension_id = "D-P"
        r.status = ReviewStatus.CONFIRMED
        r.score = Decimal(str(score))
        return r

    # 两标书得分不同但综合一致（weighted=0.5）→ 报价低优先
    rev_result = MagicMock()
    rev_result.all.return_value = [_r("BID-X", 10), _r("BID-Y", 10)]
    session.scalars.side_effect = [dim_result, rev_result]

    res = await get_lot_summary(session, lot_id="LOT-1")
    assert res["bids"][0]["bid_id"] == "BID-X"  # 100 < 110
    assert res["bids"][0]["rank"] == 1


@pytest.mark.asyncio
async def test_submit_for_award_success():
    """全部标段终态 → project=AWARDED + 触发归档 job。"""
    session = AsyncMock()
    project = MagicMock()
    project.project_id = "PRJ-1"
    project.status = "UNDER_REVIEW"
    session.get.return_value = project

    lots = [MagicMock(lot_id="LOT-1", status="EVALUATED"),
            MagicMock(lot_id="LOT-2", status="ABANDONED")]
    lot_result = MagicMock()
    lot_result.all.return_value = lots
    session.scalars.return_value = lot_result

    with patch("app.tasks.dispatch.enqueue_archive", new=AsyncMock()) as enq:
        res = await submit_for_award(session, project_id="PRJ-1", operator_id="U-1")
    assert project.status == "AWARDED"
    assert res["status"] == "AWARDED"
    enq.assert_awaited_once_with("PRJ-1")


@pytest.mark.asyncio
async def test_submit_for_award_blocks_unfinished():
    """存在未终态标段 → ProjectNotReadyError，项目不流转。"""
    session = AsyncMock()
    project = MagicMock()
    project.status = "UNDER_REVIEW"
    session.get.return_value = project

    lots = [MagicMock(lot_id="LOT-1", status="EVALUATED"),
            MagicMock(lot_id="LOT-2", status="UNDER_REVIEW")]  # 未完成
    lot_result = MagicMock()
    lot_result.all.return_value = lots
    session.scalars.return_value = lot_result

    with pytest.raises(ProjectNotReadyError) as ei:
        await submit_for_award(session, project_id="PRJ-1", operator_id="U-1")
    assert "LOT-2" in str(ei.value)
    assert project.status == "UNDER_REVIEW"


@pytest.mark.asyncio
async def test_submit_for_award_project_not_found():
    """项目不存在 → 404。"""
    session = AsyncMock()
    session.get.return_value = None
    from app.services.closeout_service import ProjectNotFoundError

    with pytest.raises(ProjectNotFoundError):
        await submit_for_award(session, project_id="PRJ-X", operator_id="U-1")

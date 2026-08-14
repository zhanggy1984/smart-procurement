"""P7.3 评审收尾 API 集成测试（task.md #13/#14）。

完整链路：3 投标 FROZEN → match → 申报 → 全部评审格提交 → complete-review
EVALUATED + 报告 PDF → submit-for-award AWARDED。
错误：有维度未完成 400；lot 非 UNDER_REVIEW 400；有 lot 未就绪 submit 400。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select, text

from app.core.database import session_factory
from app.models.bid_document import BidDocument
from app.models.lot_expert_assignment import LotExpertAssignment
from app.models.project import ScoringDimension


async def _frozen_lot(client, pm_headers, lot_factory, bid_factory, set_bid_parsed, amounts=None):
    lot = await lot_factory()
    bids = await bid_factory(lot["lot_id"])
    await set_bid_parsed(lot["lot_id"], bids)
    if amounts:
        async with session_factory() as s:
            for b, amt in zip(bids, amounts):
                await s.execute(text("UPDATE bid_document SET bid_amount=:a WHERE bid_id=:b"),
                                {"a": amt, "b": b})
            await s.commit()
    r = await client.post(f"/api/v1/lots/{lot['lot_id']}/close-bidding", headers=pm_headers)
    assert r.status_code == 200, r.text
    assert r.json()["risk"] == "LOW"
    return lot, bids


async def _match_and_declare_all(client, pm_headers, lot_id):
    """match + 全部专家申报无冲突（service 层，P7.1 场景同款）。"""
    r = await client.post(f"/api/v1/lots/{lot_id}/match-experts", headers=pm_headers,
                          json={"tags": ["软件开发"]})
    assert r.status_code == 200, r.text
    from app.services import expert_declaration_service

    async with session_factory() as s:
        suppliers = (await s.scalars(
            select(BidDocument.supplier_id).where(BidDocument.lot_id == lot_id).distinct())).all()
        assigns = (await s.scalars(
            select(LotExpertAssignment).where(LotExpertAssignment.lot_id == lot_id))).all()
        for a in assigns:
            confs = [{"supplier_id": sup, "has_conflict": False} for sup in suppliers]
            await expert_declaration_service.declare(s, assignment_id=a.id, expert_id=a.expert_id,
                                                     confirmations=confs)
        await s.commit()


async def _fill_all_reviews(lot_id):
    """service 层填满全部 bid×dim 评审格（报价公式 + 参考分），提交 CONFIRMED。"""
    from app.services import review_service

    async with session_factory() as s:
        bids = (await s.scalars(select(BidDocument).where(
            BidDocument.lot_id == lot_id, BidDocument.status == "FROZEN"))).all()
        dims = (await s.scalars(select(ScoringDimension).where(
            ScoringDimension.lot_id == lot_id))).all()
        assigns = (await s.scalars(select(LotExpertAssignment).where(
            LotExpertAssignment.lot_id == lot_id))).all()
        exp_dims = {a.expert_id: (a.dimension_ids or []) for a in assigns}
        amounts = {b.bid_id: float(b.bid_amount) for b in bids if b.bid_amount}
        for bid in bids:
            for d in dims:
                expert = next((e for e, ds in exp_dims.items() if d.dimension_id in ds), None)
                if expert is None:
                    continue
                rev = await review_service.create_review(s, expert_id=expert, bid_id=bid.bid_id,
                                                         dimension_id=d.dimension_id)
                await s.commit()
                if d.name == review_service.PRICE_DIMENSION_NAME and amounts:
                    calc = review_service._calc_price_formula(
                        amounts[bid.bid_id], float(d.max_score), list(amounts.values()))
                    score = calc["result"]["calculatedScore"]
                else:
                    score = float(d.max_score) * 0.85
                await review_service.save_score(s, review_id=rev.review_id, expert_id=expert,
                                                score=score, comment="集成测试自动评分",
                                                ai_suggestion={"score": score})
                await review_service.submit_review(s, review_id=rev.review_id, expert_id=expert)
                await s.commit()


async def _evaluated_lot(client, pm_headers, lot_factory, bid_factory, set_bid_parsed):
    """完整链路到 EVALUATED，返回 lot。"""
    lot, _ = await _frozen_lot(client, pm_headers, lot_factory, bid_factory, set_bid_parsed,
                               amounts=[100, 120, 80])
    await _match_and_declare_all(client, pm_headers, lot["lot_id"])
    await _fill_all_reviews(lot["lot_id"])
    return lot


# ==================== complete-review ====================


@pytest.mark.asyncio
async def test_complete_review_success(client, pm_headers, lot_factory, bid_factory, set_bid_parsed):
    """全部评审格完成 → complete-review → EVALUATED + 报告可下载。"""
    lot = await _evaluated_lot(client, pm_headers, lot_factory, bid_factory, set_bid_parsed)
    resp = await client.post(f"/api/v1/lots/{lot['lot_id']}/complete-review", headers=pm_headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "EVALUATED"
    # 报告 PDF
    rep = await client.get(f"/api/v1/lots/{lot['lot_id']}/summary/report", headers=pm_headers)
    assert rep.status_code == 200
    assert rep.headers["content-type"] == "application/pdf"
    assert rep.content[:4] == b"%PDF"


@pytest.mark.asyncio
async def test_complete_review_reviews_incomplete_400(client, pm_headers, lot_factory, bid_factory, set_bid_parsed):
    """有评审格未提交 → 400。"""
    lot = await _evaluated_lot(client, pm_headers, lot_factory, bid_factory, set_bid_parsed)
    # 把一条已提交评审置回 DRAFT → 存在未完成格子
    async with session_factory() as s:
        rid = (await s.execute(text(
            "SELECT r.review_id FROM expert_review r JOIN bid_document b ON r.bid_id=b.bid_id "
            "WHERE b.lot_id=:l LIMIT 1"), {"l": lot["lot_id"]})).scalar_one_or_none()
        assert rid, "应存在已提交评审"
        await s.execute(text("UPDATE expert_review SET status='DRAFT' WHERE review_id=:r"), {"r": rid})
        await s.commit()
    resp = await client.post(f"/api/v1/lots/{lot['lot_id']}/complete-review", headers=pm_headers)
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_complete_review_lot_not_under_review_400(client, pm_headers, lot_factory):
    """lot 仍 BIDDING → 400。"""
    lot = await lot_factory()
    resp = await client.post(f"/api/v1/lots/{lot['lot_id']}/complete-review", headers=pm_headers)
    assert resp.status_code == 400


# ==================== submit-for-award ====================


@pytest.mark.asyncio
async def test_submit_for_award_success(client, pm_headers, lot_factory, bid_factory, set_bid_parsed):
    """全部 lot EVALUATED → submit → AWARDED。"""
    lot = await _evaluated_lot(client, pm_headers, lot_factory, bid_factory, set_bid_parsed)
    await client.post(f"/api/v1/lots/{lot['lot_id']}/complete-review", headers=pm_headers)
    resp = await client.post(f"/api/v1/projects/{lot['project_id']}/submit-for-award",
                             headers=pm_headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "AWARDED"


@pytest.mark.asyncio
async def test_submit_for_award_not_ready_400(client, pm_headers, lot_factory, bid_factory, set_bid_parsed):
    """有 lot 未 EVALUATED → 400。"""
    lot, _ = await _frozen_lot(client, pm_headers, lot_factory, bid_factory, set_bid_parsed,
                               amounts=[100, 120, 80])
    # 未 complete-review，lot 仍 UNDER_REVIEW
    resp = await client.post(f"/api/v1/projects/{lot['project_id']}/submit-for-award",
                             headers=pm_headers)
    assert resp.status_code == 400

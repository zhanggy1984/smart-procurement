"""P7.3 围串标初筛 API 集成测试（task.md #7 close-bidding）。

成功：3 家投标无关联 → 初筛 LOW → 标书 FROZEN + lot UNDER_REVIEW（自动通过）。
错误：有效标书<3 → ABANDONED+400；lot 非 BIDDING → 400；无标书 → 400；非 PM 角色 → 403。
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.core.database import session_factory


async def _close(client, headers, lot_id):
    return await client.post(f"/api/v1/lots/{lot_id}/close-bidding", headers=headers)


async def _lot_status(lot_id: str) -> str:
    async with session_factory() as s:
        return (await s.execute(text("SELECT status FROM lot WHERE lot_id=:l"), {"l": lot_id})).scalar()


@pytest.mark.asyncio
async def test_close_bidding_low_auto_pass(client, pm_headers, lot_factory, bid_factory, set_bid_parsed):
    """3 家投标无实质关联 → LOW → 标书 FROZEN + lot UNDER_REVIEW。"""
    lot = await lot_factory()
    bids = await bid_factory(lot["lot_id"])
    await set_bid_parsed(lot["lot_id"], bids)
    resp = await _close(client, pm_headers, lot["lot_id"])
    assert resp.status_code == 200
    body = resp.json()
    assert body["risk"] == "LOW"
    assert await _lot_status(lot["lot_id"]) == "UNDER_REVIEW"
    async with session_factory() as s:
        frozen = (await s.execute(
            text("SELECT COUNT(*) FROM bid_document WHERE lot_id=:l AND status='FROZEN'"),
            {"l": lot["lot_id"]})).scalar()
    assert frozen == 3


@pytest.mark.asyncio
async def test_close_bidding_valid_lt_3_abandoned(client, pm_headers, lot_factory, bid_factory, set_bid_parsed):
    """仅 2 家有效标书 → 400 + lot ABANDONED。"""
    lot = await lot_factory()
    bids = await bid_factory(lot["lot_id"])
    await set_bid_parsed(lot["lot_id"], bids[:2])  # 只 2 家解析完成
    resp = await _close(client, pm_headers, lot["lot_id"])
    assert resp.status_code == 400
    assert await _lot_status(lot["lot_id"]) == "ABANDONED"


@pytest.mark.asyncio
async def test_close_bidding_not_bidding_400(client, pm_headers, lot_factory, bid_factory, set_bid_parsed):
    """lot 已 UNDER_REVIEW（非 BIDDING）重复关闭 → 400。"""
    lot = await lot_factory()
    bids = await bid_factory(lot["lot_id"])
    await set_bid_parsed(lot["lot_id"], bids)
    r1 = await _close(client, pm_headers, lot["lot_id"])
    assert r1.status_code == 200
    r2 = await _close(client, pm_headers, lot["lot_id"])
    assert r2.status_code == 400


@pytest.mark.asyncio
async def test_close_bidding_no_bids_400(client, pm_headers, lot_factory):
    """无标书 → 400。"""
    lot = await lot_factory()
    resp = await _close(client, pm_headers, lot["lot_id"])
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_close_bidding_forbidden_supplier_403(client, sup_headers, lot_factory):
    """非 PM/ADMIN（供应商）→ 403。"""
    lot = await lot_factory()
    resp = await _close(client, sup_headers, lot["lot_id"])
    assert resp.status_code == 403

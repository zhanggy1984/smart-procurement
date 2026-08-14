"""P7.3 供应商状态 API 集成测试（task.md #18）。

成功：拉黑 → INACTIVE + 未封存标书 DISQUALIFIED（级联）。
错误：非管理员 403；供应商不存在 404；非法参数 422。
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.core.database import session_factory


async def _blacklist(client, headers, supplier_id, blacklisted=True):
    return await client.put(f"/api/v1/suppliers/{supplier_id}/status",
                            headers=headers, json={"blacklisted": blacklisted})


@pytest.mark.asyncio
async def test_blacklist_cascades_bids(client, admin_headers, lot_factory, bid_factory):
    """拉黑 → INACTIVE + 未封存标书 DISQUALIFIED（级联验证）。"""
    lot = await lot_factory()
    await bid_factory(lot["lot_id"])  # ITEST-S1 已投标（SUBMITTED，未封存）
    resp = await _blacklist(client, admin_headers, "ITEST-S1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["blacklisted"] is True
    assert body["status"] == "INACTIVE"
    async with session_factory() as s:
        st = (await s.execute(text(
            "SELECT status FROM bid_document WHERE supplier_id='ITEST-S1'"))).scalars().all()
    assert st and all(x == "DISQUALIFIED" for x in st)


@pytest.mark.asyncio
async def test_unblacklist_restores(client, admin_headers, lot_factory, bid_factory):
    """解除拉黑 → ACTIVE。"""
    lot = await lot_factory()
    await bid_factory(lot["lot_id"])
    await _blacklist(client, admin_headers, "ITEST-S1")
    resp = await _blacklist(client, admin_headers, "ITEST-S1", blacklisted=False)
    assert resp.status_code == 200
    assert resp.json()["blacklisted"] is False
    assert resp.json()["status"] == "ACTIVE"


@pytest.mark.asyncio
async def test_blacklist_forbidden_supplier_role_403(client, sup_headers):
    resp = await _blacklist(client, sup_headers, "ITEST-S1")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_blacklist_not_found_404(client, admin_headers):
    resp = await _blacklist(client, admin_headers, "ITEST-S-MISSING")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_status_invalid_params_422(client, admin_headers):
    """blacklisted/status 均未传 → 422。"""
    resp = await client.put("/api/v1/suppliers/ITEST-S1/status",
                            headers=admin_headers, json={"blacklisted": None})
    assert resp.status_code == 422

"""P7.3 标书 API 集成测试（task.md #6）。

成功：上传 PDF → 201 + SUBMITTED + 触发解析入队（fire-and-forget）。
错误：非 PDF magic bytes 422、lot 非 BIDDING 400、lot 不存在 404、
ADMIN 缺 supplier_id 422、供应商不存在 404、重复投标 409、超 50MB 413。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text

from app.core.database import session_factory

PDF = b"%PDF-1.4\n%% itest bid content\n" + b"tech plan description " * 20


async def _upload(client, headers, lot_id, *, content=PDF, filename="bid.pdf", supplier_id=None):
    data = {"supplier_id": supplier_id} if supplier_id else None
    return await client.post(
        f"/api/v1/lots/{lot_id}/bids", headers=headers,
        files={"file": (filename, content, "application/pdf")}, data=data,
    )


async def _set_lot_status(lot_id: str, status: str) -> None:
    async with session_factory() as s:
        await s.execute(text("UPDATE lot SET status=:st WHERE lot_id=:l"), {"st": status, "l": lot_id})
        await s.commit()


@pytest.mark.asyncio
async def test_upload_bid_success_enqueues_parse(client, sup_headers, lot_factory, set_bid_parsed):
    """供应商上传 PDF → 201 + SUBMITTED + dispatch 入队（解析链路触发）。"""
    lot = await lot_factory()
    with patch("app.api.v1.bids.dispatch.enqueue_document_ingest", new=AsyncMock()) as enq:
        resp = await _upload(client, sup_headers, lot["lot_id"])
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "SUBMITTED"
    assert body["parsing_step"] == 0
    assert body["lot_id"] == lot["lot_id"]
    assert enq.await_count == 1


@pytest.mark.asyncio
async def test_upload_bid_not_pdf_422(client, sup_headers, lot_factory):
    """非 PDF/DOCX magic bytes → 422。"""
    lot = await lot_factory()
    resp = await _upload(client, sup_headers, lot["lot_id"], content=b"NOT A PDF FILE" * 10)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_upload_bid_lot_not_biddable_400(client, sup_headers, lot_factory):
    """lot 非 BIDDING（UNDER_REVIEW）→ 400。"""
    lot = await lot_factory()
    await _set_lot_status(lot["lot_id"], "UNDER_REVIEW")
    resp = await _upload(client, sup_headers, lot["lot_id"])
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_upload_bid_lot_not_found_404(client, sup_headers):
    resp = await _upload(client, sup_headers, "ITEST-LT-MISSING")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_upload_bid_admin_without_supplier_id_422(client, admin_headers, lot_factory):
    """ADMIN 代传必须显式 supplier_id。"""
    lot = await lot_factory()
    resp = await _upload(client, admin_headers, lot["lot_id"])
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_upload_bid_admin_supplier_not_found_404(client, admin_headers, lot_factory):
    lot = await lot_factory()
    resp = await _upload(client, admin_headers, lot["lot_id"], supplier_id="ITEST-S-MISSING")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_upload_bid_duplicate_409(client, sup_headers, lot_factory):
    """同一供应商重复投同一标段 → 409。"""
    lot = await lot_factory()
    r1 = await _upload(client, sup_headers, lot["lot_id"])
    assert r1.status_code == 201
    r2 = await _upload(client, sup_headers, lot["lot_id"])
    assert r2.status_code == 409


@pytest.mark.asyncio
async def test_upload_bid_too_large_413(client, sup_headers, lot_factory):
    """超 50MB → 413。"""
    lot = await lot_factory()
    big = b"0" * (50 * 1024 * 1024 + 1)
    resp = await _upload(client, sup_headers, lot["lot_id"], content=big)
    assert resp.status_code == 413


@pytest.mark.asyncio
async def test_get_bid_detail_and_status(client, sup_headers, admin_headers, lot_factory):
    """上传后详情 + 解析进度可查（评审角色可见）。"""
    lot = await lot_factory()
    up = await _upload(client, sup_headers, lot["lot_id"])
    bid_id = up.json()["bid_id"]
    detail = await client.get(f"/api/v1/bids/{bid_id}", headers=sup_headers)
    assert detail.status_code == 403  # SUPPLIER 不可看详情（限评审角色）
    status = await client.get(f"/api/v1/bids/{bid_id}/status", headers=sup_headers)
    assert status.status_code == 403
    detail = await client.get(f"/api/v1/bids/{bid_id}", headers=admin_headers)
    assert detail.status_code == 200
    assert detail.json()["status"] == "SUBMITTED"
    status = await client.get(f"/api/v1/bids/{bid_id}/status", headers=admin_headers)
    assert status.status_code == 200
    assert status.json()["parsing_step"] == 0

"""P7.2 BidDocumentService 单元测试（task.md：4 用例）。

覆盖：magic bytes 识别（PDF/OOXML/未知）、文件超限拒绝、
标段非 BIDDING 拒绝。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.bid_document_service import (
    FileTooLargeError,
    LotNotBiddableError,
    UnsupportedFileTypeError,
    _detect_type,
    upload_bid,
)


def test_detect_type_magic_bytes():
    """magic bytes：PDF → application/pdf，OOXML → docx MIME，未知 → None。"""
    assert _detect_type(b"%PDF-1.4") == "application/pdf"
    assert _detect_type(b"PK\x03\x04doc") == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    assert _detect_type(b"plain text") is None
    assert _detect_type(b"") is None


@pytest.mark.asyncio
async def test_upload_rejects_oversize():
    """>50MB → FileTooLargeError（校验优先于类型）。"""
    session = AsyncMock()
    big = b"x" * (51 * 1024 * 1024)
    with pytest.raises(FileTooLargeError):
        await upload_bid(session, lot_id="LOT-1", supplier_id="SUP-1",
                         filename="b.pdf", content=big, operator_id="op1")


@pytest.mark.asyncio
async def test_upload_rejects_bad_type():
    """非 PDF/DOCX → UnsupportedFileTypeError。"""
    session = AsyncMock()
    with pytest.raises(UnsupportedFileTypeError):
        await upload_bid(session, lot_id="LOT-1", supplier_id="SUP-1",
                         filename="b.exe", content=b"MZ\x90\x00", operator_id="op1")


@pytest.mark.asyncio
async def test_upload_rejects_lot_not_biddable():
    """标段非 BIDDING → LotNotBiddableError。"""
    session = AsyncMock()
    lot = MagicMock()
    lot.status = "UNDER_REVIEW"  # 非 BIDDING
    session.get.return_value = lot
    with pytest.raises(LotNotBiddableError):
        await upload_bid(session, lot_id="LOT-1", supplier_id="SUP-1",
                         filename="b.pdf", content=b"%PDF-1.4 legal", operator_id="op1")


def _boom_minio(*a, **kw):
    raise RuntimeError("minio down")


@pytest.mark.asyncio
async def test_get_bid_presign_failure_returns_none_url(monkeypatch):
    """MinIO 挂/超时 → get_bid 返回 (bid, None)（前端"文件暂不可用"而非 500）。"""
    import app.services.bid_document_service as bs

    session = AsyncMock()
    bid = MagicMock()
    bid.file_url = "bids/LOT-1/B1/a.pdf"
    session.get.return_value = bid

    monkeypatch.setattr(bs, "presign_url", _boom_minio)
    got, signed = await bs.get_bid(session, bid_id="B1")
    assert got is bid
    assert signed is None


@pytest.mark.asyncio
async def test_upload_bid_presign_failure_returns_none_url(monkeypatch):
    """DB 已提交但 MinIO presign 挂 → upload_bid 返回 (bid, None) 不整体 500。

    P8 异常兜底：修"DB 已提交但响应 500 让前端误判失败重传"窗口。
    """
    import app.services.bid_document_service as bs

    session = AsyncMock()
    lot = MagicMock()
    lot.status = "BIDDING"
    supplier = MagicMock()
    supplier.blacklisted = False
    supplier.status = "ACTIVE"
    supplier.name = "供应商A"
    session.get.side_effect = [lot, supplier]
    session.scalar.return_value = None  # 同标段无已投标书
    monkeypatch.setattr(bs, "get_minio_client", lambda: MagicMock())
    monkeypatch.setattr(bs, "_upload_to_minio", AsyncMock())
    monkeypatch.setattr(bs, "presign_url", _boom_minio)

    bid, signed = await bs.upload_bid(
        session, lot_id="LOT-1", supplier_id="SUP-1",
        filename="b.pdf", content=b"%PDF-1.4 legal", operator_id="op1",
    )
    assert signed is None
    assert bid.bid_id  # MySQL 记录已落库
    session.commit.assert_awaited_once()

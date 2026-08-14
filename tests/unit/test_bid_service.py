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

"""标书管理服务（P1.5）。

上传链路：magic bytes 校验（PDF/DOCX）→ MinIO 存储 → MySQL 记录
（status=SUBMITTED, parsing_step=0）。上传不写 freeze_hash：非空即"已封存"，
P1.4 拉黑级联以 `freeze_hash IS NULL` 判定"未封存可废标"，封存动作发生在
解析完成 + 围串标初筛通过后（P2），上传即写哈希会让级联永远不触发。

retry-parse：PARSE_FAILED → 重置 SUBMITTED + parsing_step=0（真实解析流水线
在 P2.1，本阶段只落地状态机骨架；触发 arq 解析亦在 P2.1）。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import generate_id
from app.core.minio_client import get_minio_client, presign_url, upload_bytes
from app.models.bid_document import BidDocument, BidStatus
from app.models.project import Lot
from app.models.supplier import Supplier, SupplierStatus

logger = structlog.get_logger(__name__)

# 上传限制（task.md P1.5：上限 50MB）
MAX_FILE_SIZE = 50 * 1024 * 1024

# magic bytes：PDF（%PDF-）、DOCX/OOXML（zip 头 PK\x03\x04）
_MAGIC_PDF = b"%PDF"
_MAGIC_OOXML = b"PK\x03\x04"

# 标段可投标状态（solution.md 状态可见性矩阵：仅 BIDDING 可上传）
_LOT_BIDDABLE = "BIDDING"


class LotNotFoundError(ValueError):
    """标段不存在 → 404。"""


class SupplierNotFoundError(ValueError):
    """供应商不存在 → 404。"""


class LotNotBiddableError(ValueError):
    """标段不在投标期 → 400。"""


class SupplierNotEligibleError(ValueError):
    """供应商被拉黑/停用，不可投标 → 400。"""


class BidAlreadyExistsError(ValueError):
    """同一标段下该供应商已投标 → 409。"""


class BidNotFoundError(ValueError):
    """标书不存在 → 404。"""


class FileTooLargeError(ValueError):
    """文件超过 50MB → 413。"""


class UnsupportedFileTypeError(ValueError):
    """非 PDF/DOCX → 422。"""


class InvalidBidStatusError(ValueError):
    """状态不允许该操作 → 400。"""


def _detect_type(content: bytes) -> Optional[str]:
    """按 magic bytes 识别 PDF / OOXML（docx）。返回 MIME，未知返回 None。"""
    if content.startswith(_MAGIC_PDF):
        return "application/pdf"
    if content.startswith(_MAGIC_OOXML):
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    return None


async def _upload_to_minio(client, object_name: str, data: bytes, mime: str) -> None:
    """MinIO 同步上传卸载到线程池（minio-py 阻塞 IO）。失败抛原始异常。"""
    await asyncio.to_thread(upload_bytes, client, object_name, data, mime)


async def upload_bid(
    session: AsyncSession,
    *,
    lot_id: str,
    supplier_id: str,
    filename: str,
    content: bytes,
    operator_id: str,
) -> tuple[BidDocument, str]:
    """上传标书：校验 → MinIO 存储 → MySQL 记录。返回（记录, 预签名 URL）。

    校验顺序：文件大小 → magic bytes → 标段存在且 BIDDING → 供应商存在且未拉黑
    → 同标段去重（409）。MinIO 先成功再写库：失败不留 DB 脏记录；
    MinIO 成功而 DB 失败留孤儿对象（对象名含 bid_id 可定位，风险可接受）。
    """
    if len(content) > MAX_FILE_SIZE:
        raise FileTooLargeError(f"文件超过上限 50MB: {len(content)} bytes")
    mime = _detect_type(content)
    if mime is None:
        raise UnsupportedFileTypeError("仅支持 PDF/DOCX（magic bytes 校验失败）")

    lot = await session.get(Lot, lot_id)
    if lot is None:
        raise LotNotFoundError(f"标段不存在: {lot_id}")
    if lot.status != _LOT_BIDDABLE:
        raise LotNotBiddableError(f"标段状态 {lot.status} 不可投标，仅 BIDDING 可上传")

    supplier = await session.get(Supplier, supplier_id)
    if supplier is None:
        raise SupplierNotFoundError(f"供应商不存在: {supplier_id}")
    if supplier.blacklisted or supplier.status != SupplierStatus.ACTIVE:
        raise SupplierNotEligibleError(f"供应商 {supplier.name} 已拉黑或停用，不可投标")

    existing = await session.scalar(
        select(BidDocument.bid_id).where(
            BidDocument.lot_id == lot_id,
            BidDocument.supplier_id == supplier_id,
        )
    )
    if existing:
        raise BidAlreadyExistsError(f"供应商 {supplier_id} 已投 {lot_id}（标书 {existing}），不可重复投标")

    # 文件名安全：取 basename，防路径穿越
    safe_name = Path(filename or "bid.pdf").name or "bid.pdf"
    bid_id = generate_id("BID")
    object_name = f"bids/{lot_id}/{bid_id}/{safe_name}"
    client = get_minio_client()
    try:
        await _upload_to_minio(client, object_name, content, mime)
    except Exception:
        logger.error("bid.minio_upload_failed", lot_id=lot_id, supplier_id=supplier_id, object_name=object_name)
        raise

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    bid = BidDocument(
        bid_id=bid_id,
        lot_id=lot_id,
        supplier_id=supplier_id,
        file_url=object_name,
        status=BidStatus.SUBMITTED,
        parsing_step=0,
        created_at=now,
        updated_at=now,
    )
    session.add(bid)
    await session.commit()
    await session.refresh(bid)

    # P8 异常兜底：presign 失败 → signed=None（DB 已提交成功，不能再整体 500 让前端误判失败重传）
    try:
        signed = await asyncio.to_thread(presign_url, client, object_name)
    except Exception as e:  # noqa: BLE001
        logger.warning("bid.upload_presign_failed", bid_id=bid_id, error=str(e))
        signed = None
    logger.info(
        "bid.uploaded",
        bid_id=bid_id,
        lot_id=lot_id,
        supplier_id=supplier_id,
        size=len(content),
        operator=operator_id,
    )
    return bid, signed


async def get_bid(session: AsyncSession, bid_id: str) -> tuple[BidDocument, Optional[str]]:
    """标书详情（含结构化数据 + 动态预签名 URL）。"""
    bid = await session.get(BidDocument, bid_id)
    if bid is None:
        raise BidNotFoundError(f"标书不存在: {bid_id}")
    signed = None
    if bid.file_url:
        # P8 异常兜底：MinIO 挂/超时 → signed=None（前端"文件暂不可用"），不整体 500
        try:
            signed = await asyncio.to_thread(presign_url, get_minio_client(), bid.file_url)
        except Exception as e:  # noqa: BLE001
            logger.warning("bid.presign_failed", bid_id=bid_id, error=str(e))
    return bid, signed


async def get_bid_status(session: AsyncSession, bid_id: str) -> BidDocument:
    """解析进度（含 parsing_step checkpoint）。"""
    bid = await session.get(BidDocument, bid_id)
    if bid is None:
        raise BidNotFoundError(f"标书不存在: {bid_id}")
    return bid


async def retry_parse(session: AsyncSession, bid_id: str, *, operator_id: str) -> BidDocument:
    """解析失败重试：PARSE_FAILED → SUBMITTED + parsing_step=0。

    真实解析流水线（arq job）在 P2.1 落地，本阶段只重置状态机。
    """
    bid = await session.get(BidDocument, bid_id)
    if bid is None:
        raise BidNotFoundError(f"标书不存在: {bid_id}")
    if bid.status != BidStatus.PARSE_FAILED:
        raise InvalidBidStatusError(f"仅解析失败（PARSE_FAILED）可重试，当前状态: {bid.status}")

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    bid.status = BidStatus.SUBMITTED
    bid.parsing_step = 0
    bid.updated_at = now
    await session.commit()
    await session.refresh(bid)
    logger.info("bid.retry_parse", bid_id=bid_id, operator=operator_id)
    return bid

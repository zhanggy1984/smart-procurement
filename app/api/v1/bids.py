"""标书管理 API（P1.5）。

- POST /lots/{lot_id}/bids        上传标书（multipart，PDF/DOCX，上限 50MB）
- GET  /bids/{bid_id}             标书详情（含结构化数据 + 30min 预签名 URL）
- GET  /bids/{bid_id}/status      解析进度（含 parsing_step checkpoint）
- POST /bids/{bid_id}/retry-parse 解析失败手动重试（重置状态机）

权限：上传限 SUPPLIER/ADMIN（SUPPLIER 只能投本供应商标书，按 display_name
绑定主体；ADMIN 显式传 supplier_id 代传）；详情/进度限评审相关角色
（ADMIN/PM/REVIEW_EXPERT，评审上下文可见）；retry-parse 限 ADMIN。

供应商主体绑定说明：supplier 表无 user_id 列（P0.4 DDL 已定），登录账号与
供应商实体按 users.display_name = supplier.name 约定关联。合成数据存在同名
供应商（如 SUP-009/SUP-013），故 display_name 反查可能多值——SUPPLIER 用户
未显式传 supplier_id 且匹配多个时返回 422 提示显式指定。
"""

from typing import Optional

import asyncio
import structlog
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_roles
from app.core.database import get_db_session
from app.models.bid_document import BidDocument
from app.models.project import Lot
from app.models.supplier import Supplier
from app.models.user import Role, User
from app.schemas.bid import BidOut, BidStatusOut, BidUploadResult
from app.services import bid_document_service as svc
from app.tasks import dispatch

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["bids"])


def _service_error_to_http(exc: Exception) -> HTTPException:
    """service 业务异常 → HTTP 状态映射。"""
    mapping = {
        svc.LotNotFoundError: status.HTTP_404_NOT_FOUND,
        svc.SupplierNotFoundError: status.HTTP_404_NOT_FOUND,
        svc.BidNotFoundError: status.HTTP_404_NOT_FOUND,
        svc.LotNotBiddableError: status.HTTP_400_BAD_REQUEST,
        svc.SupplierNotEligibleError: status.HTTP_400_BAD_REQUEST,
        svc.BidAlreadyExistsError: status.HTTP_409_CONFLICT,
        svc.FileTooLargeError: status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        svc.UnsupportedFileTypeError: status.HTTP_422_UNPROCESSABLE_ENTITY,
        svc.InvalidBidStatusError: status.HTTP_400_BAD_REQUEST,
    }
    http_status = mapping.get(type(exc), status.HTTP_422_UNPROCESSABLE_ENTITY)
    return HTTPException(status_code=http_status, detail=str(exc))


async def _resolve_supplier(
    session: AsyncSession,
    user: User,
    supplier_id: Optional[str],
) -> Supplier:
    """解析投标主体：ADMIN 必须显式指定；SUPPLIER 按 display_name 绑定（唯一）。

    SUPPLIER 显式传 supplier_id 时校验属于本供应商（防代投）；未传且
    display_name 匹配多个供应商（合成数据重名）→ 422 提示显式指定。
    """
    if user.role == Role.ADMIN:
        if not supplier_id:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="ADMIN 上传必须显式传 supplier_id")
        supplier = await session.get(Supplier, supplier_id)
        if supplier is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"供应商不存在: {supplier_id}")
        return supplier

    # SUPPLIER 角色：display_name 绑定供应商主体
    candidates = (
        await session.scalars(select(Supplier).where(Supplier.name == user.display_name))
    ).all()
    if supplier_id:
        supplier = next((s for s in candidates if s.supplier_id == supplier_id), None)
        if supplier is None:
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="不能代投非本供应商的标书")
        return supplier
    if len(candidates) != 1:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="当前账号对应多个供应商，请显式传 supplier_id",
        )
    return candidates[0]


@router.post(
    "/lots/{lot_id}/bids",
    response_model=BidUploadResult,
    status_code=status.HTTP_201_CREATED,
    summary="上传标书（PDF/DOCX，上限 50MB）",
)
async def upload_bid(
    lot_id: str,
    file: UploadFile = File(...),
    supplier_id: Optional[str] = Form(None, description="投标主体（SUPPLIER 默认按账号绑定，ADMIN 必填）"),
    session: AsyncSession = Depends(get_db_session),
    user: User = Depends(require_roles(Role.ADMIN, Role.SUPPLIER)),
) -> BidUploadResult:
    logger.debug("bid.upload_request", operator=user.user_id, lot_id=lot_id, filename=file.filename, supplier_id=supplier_id)
    content = await file.read()
    if not content:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="上传文件为空")
    supplier = await _resolve_supplier(session, user, supplier_id)
    try:
        bid, signed = await svc.upload_bid(
            session,
            lot_id=lot_id,
            supplier_id=supplier.supplier_id,
            filename=file.filename or "bid.pdf",
            content=content,
            operator_id=user.user_id,
        )
    except (svc.LotNotFoundError, svc.SupplierNotFoundError, svc.LotNotBiddableError,
            svc.SupplierNotEligibleError, svc.BidAlreadyExistsError, svc.FileTooLargeError,
            svc.UnsupportedFileTypeError) as e:
        logger.info("bid.upload_failed", error=str(e), lot_id=lot_id, supplier_id=supplier.supplier_id)
        raise _service_error_to_http(e)
    logger.info("bid.upload_success", bid_id=bid.bid_id, lot_id=lot_id, supplier_id=bid.supplier_id)
    # P2.1：上传成功即触发异步解析（fire-and-forget，投递失败不影响上传结果）
    await dispatch.enqueue_document_ingest(bid.bid_id)
    return BidUploadResult(
        bid_id=bid.bid_id,
        lot_id=bid.lot_id,
        supplier_id=bid.supplier_id,
        filename=file.filename or "bid.pdf",
        status=bid.status,
        parsing_step=bid.parsing_step or 0,
        file_url=bid.file_url or "",
        presigned_url=signed,
    )


@router.get("/lots/{lot_id}/bids", summary="标段下标书列表（P6.3）")
async def list_lot_bids(
    lot_id: str,
    session: AsyncSession = Depends(get_db_session),
    _user: User = Depends(require_roles(Role.ADMIN, Role.PROJECT_MANAGER, Role.REVIEW_EXPERT)),
) -> dict:
    logger.debug("bid.list_by_lot_request", lot_id=lot_id)
    lot = await session.get(Lot, lot_id)
    if lot is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"标段不存在: {lot_id}")
    rows = (
        await session.execute(
            select(BidDocument, Supplier)
            .join(Supplier, BidDocument.supplier_id == Supplier.supplier_id)
            .where(BidDocument.lot_id == lot_id)
            .order_by(BidDocument.created_at)
        )
    ).all()
    items = []
    for bid, supplier in rows:
        item = BidOut.model_validate(bid).model_dump()
        item["supplier_name"] = supplier.name
        items.append(item)
    logger.info("bid.list_by_lot_success", lot_id=lot_id, count=len(items))
    return {"items": items, "total": len(items)}


@router.get("/bids/{bid_id}", response_model=BidOut, summary="标书详情（含结构化数据）")
async def get_bid(
    bid_id: str,
    session: AsyncSession = Depends(get_db_session),
    user: User = Depends(require_roles(Role.ADMIN, Role.PROJECT_MANAGER, Role.REVIEW_EXPERT)),
) -> BidOut:
    logger.debug("bid.detail_request", operator=user.user_id, bid_id=bid_id)
    try:
        bid, signed = await svc.get_bid(session, bid_id)
    except svc.BidNotFoundError as e:
        raise _service_error_to_http(e)
    out = BidOut.model_validate(bid)
    out.presigned_url = signed
    logger.info("bid.detail_success", bid_id=bid_id)
    return out


@router.get("/bids/{bid_id}/status", response_model=BidStatusOut, summary="标书解析进度")
async def get_bid_status(
    bid_id: str,
    session: AsyncSession = Depends(get_db_session),
    user: User = Depends(require_roles(Role.ADMIN, Role.PROJECT_MANAGER, Role.REVIEW_EXPERT)),
) -> BidStatusOut:
    logger.debug("bid.status_request", operator=user.user_id, bid_id=bid_id)
    try:
        bid = await svc.get_bid_status(session, bid_id)
    except svc.BidNotFoundError as e:
        raise _service_error_to_http(e)
    logger.info("bid.status_success", bid_id=bid_id, status=bid.status, parsing_step=bid.parsing_step)
    return BidStatusOut.model_validate(bid)


@router.get("/bids/{bid_id}/content", summary="标书内容（结构化字段 + 正文 chunks，评审工作台用）")
async def get_bid_content(
    bid_id: str,
    session: AsyncSession = Depends(get_db_session),
    user: User = Depends(require_roles(Role.ADMIN, Role.PROJECT_MANAGER, Role.REVIEW_EXPERT)),
) -> dict:
    """评审工作台左栏标书内容：结构化卡片 + 分章正文（Milvus chunks）。

    Milvus 不可用/无 chunks 时降级返回空列表（前端展示降级提示，不影响评分主链路）。
    """
    logger.debug("bid.content_request", operator=user.user_id, bid_id=bid_id)
    bid = await session.get(BidDocument, bid_id)
    if bid is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"标书不存在: {bid_id}")
    supplier = await session.get(Supplier, bid.supplier_id)
    lot = await session.get(Lot, bid.lot_id)

    def _fetch_chunks() -> list[dict]:
        from app.core.milvus import get_collection

        collection = get_collection()
        collection.load()
        return collection.query(
            expr=f'lot_id == "{bid.lot_id}" && bid_id == "{bid.bid_id}"',
            output_fields=["chunk_id", "content", "chapter_title", "chunk_index"],
            limit=1024,
        )

    try:
        chunks = await asyncio.to_thread(_fetch_chunks)
    except Exception as e:  # noqa: BLE001  Milvus 瞬时故障降级为空正文，不阻断
        logger.warning("bid.content_milvus_fail", bid_id=bid_id, error=str(e))
        chunks = []
    chunks = sorted(chunks, key=lambda c: c.get("chunk_index", 0))
    logger.info("bid.content_success", bid_id=bid_id, chunks=len(chunks))
    return {
        "bid_id": bid.bid_id,
        "lot_id": bid.lot_id,
        "lot_name": lot.name if lot else None,
        "supplier_id": bid.supplier_id,
        "supplier_name": supplier.name if supplier else None,
        "status": bid.status,
        "bid_amount": bid.bid_amount,
        "duration": bid.duration,
        "team_size": bid.team_size,
        "structured_data": bid.structured_data,
        "chunks": [
            {
                "chunk_id": c["chunk_id"],
                "chapter_title": c.get("chapter_title", ""),
                "chunk_index": c.get("chunk_index", 0),
                "content": c["content"],
            }
            for c in chunks
        ],
    }


@router.post("/bids/{bid_id}/retry-parse", response_model=BidStatusOut, summary="解析失败后手动重试")
async def retry_parse(
    bid_id: str,
    session: AsyncSession = Depends(get_db_session),
    operator: User = Depends(require_roles(Role.ADMIN)),
) -> BidStatusOut:
    logger.debug("bid.retry_parse_request", operator=operator.user_id, bid_id=bid_id)
    try:
        bid = await svc.retry_parse(session, bid_id, operator_id=operator.user_id)
    except (svc.BidNotFoundError, svc.InvalidBidStatusError) as e:
        raise _service_error_to_http(e)
    logger.info("bid.retry_parse_success", bid_id=bid_id, status=bid.status)
    # P2.1：重试成功后重新触发异步解析
    await dispatch.enqueue_document_ingest(bid_id)
    return BidStatusOut.model_validate(bid)

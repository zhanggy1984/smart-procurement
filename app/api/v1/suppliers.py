"""供应商管理 API（P1.4 + P6.5）。

管理侧（P1.4，限 ADMIN）：
- POST /suppliers/import（Excel 批量导入，自动建登录账号 + 冷数据唤醒）
- PUT /suppliers/{id}/status（拉黑/解除/停用/启用，拉黑触发黑名单级联）

供应商端自助（P6.5，限 SUPPLIER）：
- GET /suppliers/me/market        招标市场（可投标标段，支持类型/地区/预算筛选）
- GET /suppliers/me/bids          我的投标列表（含解析状态）
- GET /suppliers/me/bids/{id}     我的标书详情（结构化信息 + 解析结果，归属校验）
- GET /suppliers/me/results       投标结果（三态：已中标/未中标/评审中）

拉黑语义：blacklisted=true → 未封存标书 DISQUALIFIED
+ 非 AWARDED 项目评审 SUSPENDED（task.md P1.4 验收）。
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_roles
from app.core import importer
from app.core.database import get_db_session
from app.models.user import Role, User
from app.schemas.supplier import SupplierImportResult, SupplierOut, SupplierStatusUpdate
from app.services import bid_document_service
from app.services import supplier_service

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["suppliers"])


def _service_error_to_http(exc: Exception) -> HTTPException:
    """service 业务异常 → HTTP 状态映射。"""
    mapping = {
        supplier_service.SupplierNotFoundError: status.HTTP_404_NOT_FOUND,
        supplier_service.InvalidSupplierStatusError: status.HTTP_422_UNPROCESSABLE_ENTITY,
        supplier_service.SupplierNotResolvableError: status.HTTP_422_UNPROCESSABLE_ENTITY,
        bid_document_service.BidNotFoundError: status.HTTP_404_NOT_FOUND,
    }
    http_status = mapping.get(type(exc), status.HTTP_422_UNPROCESSABLE_ENTITY)
    return HTTPException(status_code=http_status, detail=str(exc))


@router.post(
    "/suppliers/import",
    response_model=SupplierImportResult,
    status_code=status.HTTP_201_CREATED,
    summary="供应商批量导入（Excel）",
)
async def import_suppliers(
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_db_session),
    operator: User = Depends(require_roles(Role.ADMIN)),
) -> SupplierImportResult:
    logger.debug("supplier.import_request", operator=operator.user_id, filename=file.filename)
    content = await file.read()
    if not content:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="上传文件为空")
    try:
        rows = importer.parse_supplier_excel(content)
    except importer.ImportFormatError as e:
        logger.info("supplier.import_format_error", error=str(e))
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))
    except importer.ImportEmptyError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    try:
        result = await supplier_service.import_suppliers(session, rows, operator_id=operator.user_id)
    except supplier_service.SupplierImportError as e:
        logger.info("supplier.import_validation_failed", errors=e.errors)
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=e.errors)
    logger.info("supplier.import_success", imported=result["imported"], skipped=result["skipped"])
    return SupplierImportResult(**result)


@router.get("/suppliers", summary="供应商列表（管理端，拉黑管理 P7.4 补齐）")
async def list_suppliers(
    keyword: str | None = None,
    page: int = 1,
    page_size: int = 20,
    session: AsyncSession = Depends(get_db_session),
    user: User = Depends(require_roles(Role.ADMIN)),
) -> dict:
    logger.debug("supplier.list_request", operator=user.user_id, keyword=keyword)
    return await supplier_service.list_suppliers(
        session, keyword=keyword, page=page, page_size=page_size
    )


@router.put(
    "/suppliers/{supplier_id}/status",
    response_model=SupplierOut,
    summary="变更供应商状态（拉黑/解除/停用/启用）",
)
async def update_status(
    supplier_id: str,
    body: SupplierStatusUpdate,
    session: AsyncSession = Depends(get_db_session),
    operator: User = Depends(require_roles(Role.ADMIN)),
) -> SupplierOut:
    logger.debug(
        "supplier.status_request",
        operator=operator.user_id,
        supplier_id=supplier_id,
        blacklisted=body.blacklisted,
        status=body.status,
    )
    try:
        supplier = await supplier_service.update_status(
            session,
            supplier_id,
            blacklisted=body.blacklisted,
            status=body.status,
            operator_id=operator.user_id,
        )
    except (supplier_service.SupplierNotFoundError, supplier_service.InvalidSupplierStatusError) as e:
        logger.info("supplier.status_failed", error=str(e))
        raise _service_error_to_http(e)
    logger.info("supplier.status_success", supplier_id=supplier_id, blacklisted=supplier.blacklisted)
    return SupplierOut.model_validate(supplier)


# ---------- 供应商端自助（P6.5） ----------


async def _resolve_me(
    session: AsyncSession,
    user: User,
) -> supplier_service.Supplier:
    """解析当前登录供应商主体（display_name → supplier，0/多值抛 422）。"""
    try:
        return await supplier_service.resolve_me(session, user)
    except supplier_service.SupplierNotResolvableError as e:
        logger.info("supplier.resolve_me_failed", operator=user.user_id, error=str(e))
        raise _service_error_to_http(e)


@router.get("/suppliers/me/market", summary="招标市场列表（供应商端，支持类型/地区/预算筛选）")
async def market(
    project_type: str | None = None,
    region: str | None = None,
    budget_min: float | None = None,
    budget_max: float | None = None,
    page: int = 1,
    page_size: int = 20,
    session: AsyncSession = Depends(get_db_session),
    user: User = Depends(require_roles(Role.SUPPLIER)),
) -> dict:
    logger.debug(
        "supplier.market_request",
        operator=user.user_id,
        type=project_type,
        region=region,
        budget_min=budget_min,
        budget_max=budget_max,
        page=page,
    )
    supplier = await _resolve_me(session, user)
    items, total, filters = await supplier_service.list_market(
        session,
        supplier.supplier_id,
        project_type=project_type,
        region=region,
        budget_min=budget_min,
        budget_max=budget_max,
        page=page,
        page_size=page_size,
    )
    logger.info("supplier.market_success", supplier_id=supplier.supplier_id, total=total)
    return {"items": items, "total": total, "page": page, "page_size": page_size, "filters": filters}


@router.get("/suppliers/me/bids", summary="我的投标列表（供应商端，含解析状态）")
async def my_bids(
    page: int = 1,
    page_size: int = 20,
    session: AsyncSession = Depends(get_db_session),
    user: User = Depends(require_roles(Role.SUPPLIER)),
) -> dict:
    logger.debug("supplier.bids_request", operator=user.user_id, page=page)
    supplier = await _resolve_me(session, user)
    items, total = await supplier_service.list_my_bids(
        session, supplier.supplier_id, page=page, page_size=page_size
    )
    logger.info("supplier.bids_success", supplier_id=supplier.supplier_id, total=total)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/suppliers/me/bids/{bid_id}", summary="我的标书详情（供应商端，结构化信息 + 解析状态）")
async def my_bid_detail(
    bid_id: str,
    session: AsyncSession = Depends(get_db_session),
    user: User = Depends(require_roles(Role.SUPPLIER)),
) -> dict:
    logger.debug("supplier.bid_detail_request", operator=user.user_id, bid_id=bid_id)
    supplier = await _resolve_me(session, user)
    try:
        detail = await supplier_service.get_my_bid_detail(session, supplier.supplier_id, bid_id)
    except bid_document_service.BidNotFoundError as e:
        logger.info("supplier.bid_detail_not_found", bid_id=bid_id, error=str(e))
        raise _service_error_to_http(e)
    logger.info("supplier.bid_detail_success", supplier_id=supplier.supplier_id, bid_id=bid_id)
    return detail


@router.get("/suppliers/me/results", summary="投标结果列表（供应商端，三态：已中标/未中标/评审中）")
async def my_results(
    session: AsyncSession = Depends(get_db_session),
    user: User = Depends(require_roles(Role.SUPPLIER)),
) -> dict:
    logger.debug("supplier.results_request", operator=user.user_id)
    supplier = await _resolve_me(session, user)
    items = await supplier_service.list_my_results(session, supplier.supplier_id)
    logger.info("supplier.results_success", supplier_id=supplier.supplier_id, count=len(items))
    return {"items": items, "total": len(items)}

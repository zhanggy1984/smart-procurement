"""企查查冲突导入 API（P1.4）。

- POST /conflicts/import（企查查 CSV，含冷数据唤醒逻辑）

限 ADMIN。失败语义：空文件 400、格式错误（缺列/编码）422；
CSV 内值非法（未知关系类型等）不走 422，计入结果计数——企查查真实数据噪声大。
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_roles
from app.core import importer
from app.core.database import get_db_session
from app.models.user import Role, User
from app.schemas.conflict import ConflictImportResult
from app.services import conflict_service

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["conflicts"])


@router.get("/pending-conflicts", summary="工商信息冷数据列表（管理端，分页）")
async def list_pending_conflicts(
    status: str | None = None,
    page: int = 1,
    page_size: int = 20,
    session: AsyncSession = Depends(get_db_session),
    user: User = Depends(require_roles(Role.ADMIN)),
) -> dict:
    logger.debug("conflict.pending_list_request", operator=user.user_id, status=status)
    return await conflict_service.list_pending_conflicts(
        session, status=status, page=page, page_size=page_size
    )


@router.post(
    "/conflicts/import",
    response_model=ConflictImportResult,
    status_code=status.HTTP_201_CREATED,
    summary="企查查冲突关系批量导入（CSV）",
)
async def import_conflicts(
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_db_session),
    operator: User = Depends(require_roles(Role.ADMIN)),
) -> ConflictImportResult:
    logger.debug("conflict.import_request", operator=operator.user_id, filename=file.filename)
    content = await file.read()
    if not content:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="上传文件为空")
    try:
        rows = importer.parse_conflict_csv(content)
    except importer.ImportFormatError as e:
        logger.info("conflict.import_format_error", error=str(e))
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))
    except importer.ImportEmptyError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    result = await conflict_service.import_conflicts(session, rows, operator_id=operator.user_id)
    logger.info(
        "conflict.import_success",
        total=result["total"],
        matched=result["matched"],
        pending=result["pending"],
    )
    return ConflictImportResult(**result)

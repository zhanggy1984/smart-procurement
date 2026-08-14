"""专家管理 API（P1.4）。

- POST /experts/import（Excel 批量导入，自动建登录账号）
- PUT /experts/{id}/status（启用/停用/拉黑）
- DELETE /experts/{id}（逻辑删除→INACTIVE）

导入/状态管理限 ADMIN。导入失败语义：空文件 400、格式错误 422、
行级校验失败 422（detail 为行错误列表，整批不导入）。
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_roles
from app.core import importer
from app.core.database import get_db_session
from app.models.expert import ExpertSpecialization
from app.models.user import Role, User
from app.schemas.expert import ExpertImportResult, ExpertOut, ExpertStatusUpdate
from app.services import expert_service

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["experts"])


@router.get("/experts", summary="专家列表（管理端，分页）")
async def list_experts(
    keyword: str | None = None,
    page: int = 1,
    page_size: int = 20,
    session: AsyncSession = Depends(get_db_session),
    user: User = Depends(require_roles(Role.ADMIN)),
) -> dict:
    logger.debug("expert.list_request", operator=user.user_id, keyword=keyword)
    return await expert_service.list_experts(
        session, keyword=keyword, page=page, page_size=page_size
    )


def _service_error_to_http(exc: Exception) -> HTTPException:
    """service 业务异常 → HTTP 状态映射。"""
    mapping = {
        expert_service.ExpertNotFoundError: status.HTTP_404_NOT_FOUND,
        expert_service.InvalidExpertStatusError: status.HTTP_422_UNPROCESSABLE_ENTITY,
    }
    http_status = mapping.get(type(exc), status.HTTP_422_UNPROCESSABLE_ENTITY)
    return HTTPException(status_code=http_status, detail=str(exc))


async def _expert_out(session: AsyncSession, expert) -> ExpertOut:
    """组装专家响应（tags 显式查询，模型无 relationship 懒加载）。"""
    out = ExpertOut.model_validate(expert)
    out.tags = list(
        (await session.scalars(select(ExpertSpecialization.tag).where(ExpertSpecialization.expert_id == expert.expert_id))).all()
    )
    return out


@router.post(
    "/experts/import",
    response_model=ExpertImportResult,
    status_code=status.HTTP_201_CREATED,
    summary="专家批量导入（Excel）",
)
async def import_experts(
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_db_session),
    operator: User = Depends(require_roles(Role.ADMIN)),
) -> ExpertImportResult:
    logger.debug("expert.import_request", operator=operator.user_id, filename=file.filename)
    content = await file.read()
    if not content:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="上传文件为空")
    try:
        rows = importer.parse_expert_excel(content)
    except importer.ImportFormatError as e:
        logger.info("expert.import_format_error", error=str(e))
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))
    except importer.ImportEmptyError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    try:
        result = await expert_service.import_experts(session, rows, operator_id=operator.user_id)
    except expert_service.ExpertImportError as e:
        logger.info("expert.import_validation_failed", errors=e.errors)
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=e.errors)
    logger.info("expert.import_success", imported=result["imported"], skipped=result["skipped"])
    return ExpertImportResult(**result)


@router.put(
    "/experts/{expert_id}/status",
    response_model=ExpertOut,
    summary="变更专家状态（启用/停用/拉黑）",
)
async def update_status(
    expert_id: str,
    body: ExpertStatusUpdate,
    session: AsyncSession = Depends(get_db_session),
    operator: User = Depends(require_roles(Role.ADMIN)),
) -> ExpertOut:
    logger.debug("expert.status_request", operator=operator.user_id, expert_id=expert_id, status=body.status)
    try:
        expert = await expert_service.update_status(
            session, expert_id, body.status, operator_id=operator.user_id
        )
    except (expert_service.ExpertNotFoundError, expert_service.InvalidExpertStatusError) as e:
        logger.info("expert.status_failed", error=str(e))
        raise _service_error_to_http(e)
    logger.info("expert.status_success", expert_id=expert_id, status=body.status)
    return await _expert_out(session, expert)


@router.delete(
    "/experts/{expert_id}",
    response_model=ExpertOut,
    summary="删除专家（逻辑删除→INACTIVE）",
)
async def delete_expert(
    expert_id: str,
    session: AsyncSession = Depends(get_db_session),
    operator: User = Depends(require_roles(Role.ADMIN)),
) -> ExpertOut:
    logger.debug("expert.delete_request", operator=operator.user_id, expert_id=expert_id)
    try:
        expert = await expert_service.delete_expert(session, expert_id, operator_id=operator.user_id)
    except expert_service.ExpertNotFoundError as e:
        logger.info("expert.delete_failed", error=str(e))
        raise _service_error_to_http(e)
    logger.info("expert.delete_success", expert_id=expert_id)
    return await _expert_out(session, expert)

"""项目管理 API（P1.3）。

- POST /projects（创建项目，校验 region/type 受控值）
- GET /projects/{id}（详情，含标段）
- POST /projects/{id}/lots（创建标段，校验 SUM(lot.budget)≤project.budget）
- POST /lots/{id}/dimensions（配置维度，校验 SUM(weight)=1.0±0.001）
- POST /lots/{id}/expert-criteria（配置遴选，校验权重和 + expert_count≥min）

创建/配置类操作限 ADMIN + PROJECT_MANAGER；详情查询任意已登录用户。
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_roles
from app.core.database import get_db_session
from app.models.project import Lot, ScoringCriterion, ScoringDimension
from app.models.user import Role, User
from app.schemas.project import (
    DimensionOut,
    DimensionsCreateRequest,
    ExpertCriteriaCreate,
    ExpertCriteriaOut,
    LotCreate,
    LotItem,
    LotOut,
    ProjectCreate,
    ProjectOut,
)
from app.services import project_service

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["projects"])

# 项目管理角色白名单
_PROJECT_ROLES = (Role.ADMIN, Role.PROJECT_MANAGER)


def _service_error_to_http(exc: Exception) -> HTTPException:
    """service 业务异常 → HTTP 状态映射。"""
    mapping = {
        project_service.ProjectCodeTakenError: status.HTTP_409_CONFLICT,
        project_service.ProjectNotFoundError: status.HTTP_404_NOT_FOUND,
        project_service.LotNotFoundError: status.HTTP_404_NOT_FOUND,
        project_service.BudgetExceededError: status.HTTP_422_UNPROCESSABLE_ENTITY,
        project_service.WeightSumError: status.HTTP_422_UNPROCESSABLE_ENTITY,
        project_service.ExpertCriteriaError: status.HTTP_422_UNPROCESSABLE_ENTITY,
    }
    http_status = mapping.get(type(exc), status.HTTP_422_UNPROCESSABLE_ENTITY)
    return HTTPException(status_code=http_status, detail=str(exc))


@router.post(
    "/projects",
    response_model=ProjectOut,
    status_code=status.HTTP_201_CREATED,
    summary="创建项目",
)
async def create_project(
    body: ProjectCreate,
    session: AsyncSession = Depends(get_db_session),
    operator: User = Depends(require_roles(*_PROJECT_ROLES)),
) -> ProjectOut:
    logger.debug("project.create_request", operator=operator.user_id, code=body.project_code)
    try:
        project = await project_service.create_project(session, body, operator_id=operator.user_id)
    except project_service.ProjectCodeTakenError as e:
        logger.info("project.create_conflict", error=str(e))
        raise _service_error_to_http(e)
    logger.info("project.create_success", project_id=project.project_id)
    return ProjectOut.model_validate(project)


@router.get("/projects/{project_id}", response_model=ProjectOut, summary="项目详情")
async def get_project(
    project_id: str,
    session: AsyncSession = Depends(get_db_session),
    _current: User = Depends(get_current_user),
) -> ProjectOut:
    project = await project_service.get_project(session, project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"项目不存在: {project_id}")
    # 显式查标段填充（模型无 relationship，避免 async 懒加载 MissingGreenlet）
    lots = (await session.scalars(select(Lot).where(Lot.project_id == project_id))).all()
    out = ProjectOut.model_validate(project)
    out.lots = [LotItem.model_validate(l) for l in lots]
    return out


@router.get("/projects", summary="项目列表（分页）")
async def list_projects(
    page: int = 1,
    page_size: int = 20,
    keyword: str | None = None,
    session: AsyncSession = Depends(get_db_session),
    _current: User = Depends(require_roles(*_PROJECT_ROLES)),
) -> dict:
    projects, total = await project_service.list_projects(
        session, page=page, page_size=page_size, keyword=keyword
    )
    return {
        "items": [ProjectOut.model_validate(p).model_dump() for p in projects],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/lots", summary="标段列表（分页，可过滤状态）")
async def list_lots(
    page: int = 1,
    page_size: int = 20,
    status: str | None = None,
    session: AsyncSession = Depends(get_db_session),
    _user: User = Depends(require_roles(*_PROJECT_ROLES)),
) -> dict:
    items, total = await project_service.list_lots(
        session, page=page, page_size=page_size, status=status
    )
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/lots/{lot_id}", response_model=LotOut, summary="标段详情（基础信息，任意登录，供应商标段详情页用）")
async def get_lot(
    lot_id: str,
    session: AsyncSession = Depends(get_db_session),
    _current: User = Depends(get_current_user),
) -> LotOut:
    """标段基础信息（预算/状态/编号）。供应商端标段详情页数据源。"""
    lot = await session.get(Lot, lot_id)
    if lot is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"标段不存在: {lot_id}")
    return LotOut.model_validate(lot)


@router.get("/lots/{lot_id}/dimensions", summary="标段评分维度（含评分标准，评审工作台/标段详情用）")
async def list_lot_dimensions(
    lot_id: str,
    session: AsyncSession = Depends(get_db_session),
    _current: User = Depends(get_current_user),
) -> dict:
    """返回标段全部评分维度 + 各维度评分标准子项。任意已登录角色可读。"""
    logger.debug("project.dimensions_list_request", lot_id=lot_id)
    lot = await session.get(Lot, lot_id)
    if lot is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"标段不存在: {lot_id}")
    dims = (
        await session.scalars(
            select(ScoringDimension)
            .where(ScoringDimension.lot_id == lot_id)
            .order_by(ScoringDimension.sort_order)
        )
    ).all()
    items = []
    for d in dims:
        criteria = (
            await session.scalars(
                select(ScoringCriterion)
                .where(ScoringCriterion.dimension_id == d.dimension_id)
                .order_by(ScoringCriterion.sort_order)
            )
        ).all()
        items.append(
            {
                "dimension_id": d.dimension_id,
                "name": d.name,
                "max_score": d.max_score,
                "weight": d.weight,
                "sort_order": d.sort_order,
                "criteria": [
                    {
                        "criterion_id": c.criterion_id,
                        "name": c.name,
                        "description": c.description,
                        "scoring_rubric": c.scoring_rubric,
                        "max_score": c.max_score,
                    }
                    for c in criteria
                ],
            }
        )
    return {"items": items, "total": len(items)}


@router.post(
    "/projects/{project_id}/lots",
    response_model=LotOut,
    status_code=status.HTTP_201_CREATED,
    summary="创建标段",
)
async def create_lot(
    project_id: str,
    body: LotCreate,
    session: AsyncSession = Depends(get_db_session),
    operator: User = Depends(require_roles(*_PROJECT_ROLES)),
) -> LotOut:
    logger.debug("project.lot_create_request", operator=operator.user_id, project_id=project_id)
    try:
        lot = await project_service.create_lot(session, project_id, body)
    except (
        project_service.ProjectNotFoundError,
        project_service.BudgetExceededError,
    ) as e:
        logger.info("project.lot_create_failed", error=str(e))
        raise _service_error_to_http(e)
    logger.info("project.lot_create_success", lot_id=lot.lot_id)
    return LotOut.model_validate(lot)


@router.post(
    "/lots/{lot_id}/dimensions",
    response_model=list[DimensionOut],
    status_code=status.HTTP_201_CREATED,
    summary="配置评分维度",
)
async def add_dimensions(
    lot_id: str,
    body: DimensionsCreateRequest,
    session: AsyncSession = Depends(get_db_session),
    operator: User = Depends(require_roles(*_PROJECT_ROLES)),
) -> list[DimensionOut]:
    logger.debug("project.dimensions_request", operator=operator.user_id, lot_id=lot_id)
    try:
        dimensions = await project_service.add_dimensions(session, lot_id, body.dimensions)
    except (
        project_service.LotNotFoundError,
        project_service.WeightSumError,
    ) as e:
        logger.info("project.dimensions_failed", error=str(e))
        raise _service_error_to_http(e)
    logger.info("project.dimensions_success", lot_id=lot_id, count=len(dimensions))
    return [DimensionOut.model_validate(d) for d in dimensions]


@router.post(
    "/lots/{lot_id}/expert-criteria",
    response_model=ExpertCriteriaOut,
    status_code=status.HTTP_201_CREATED,
    summary="配置专家遴选参数",
)
async def configure_expert_criteria(
    lot_id: str,
    body: ExpertCriteriaCreate,
    session: AsyncSession = Depends(get_db_session),
    operator: User = Depends(require_roles(*_PROJECT_ROLES)),
) -> ExpertCriteriaOut:
    logger.debug("project.expert_criteria_request", operator=operator.user_id, lot_id=lot_id)
    try:
        criteria = await project_service.configure_expert_criteria(session, lot_id, body)
    except (
        project_service.LotNotFoundError,
        project_service.WeightSumError,
        project_service.ExpertCriteriaError,
    ) as e:
        logger.info("project.expert_criteria_failed", error=str(e))
        raise _service_error_to_http(e)
    logger.info("project.expert_criteria_success", lot_id=lot_id)
    return ExpertCriteriaOut.model_validate(criteria)

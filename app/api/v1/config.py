"""系统配置 API（P6.2）。

- GET /config    配置项列表（含当前值/默认值/是否已接入业务）
- PUT /config    批量更新（校验 key 合法 + value 区间，非法 422）

权限：仅 ADMIN。写入走 config_service（UPSERT system_config + 内存缓存），
业务侧读取零 DB 查询，保存后即时生效。
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_roles
from app.core.database import get_db_session
from app.models.user import Role, User
from app.services import config_service

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["config"])


class ConfigItem(BaseModel):
    """单条配置更新。value 统一按数字校验。"""

    model_config = ConfigDict(extra="forbid")

    key: str
    value: float


class ConfigUpdateRequest(BaseModel):
    """PUT /config 请求体：批量更新。"""

    model_config = ConfigDict(extra="forbid")

    items: list[ConfigItem]


@router.get("/config", summary="系统配置列表（管理端）")
async def list_config(_admin: User = Depends(require_roles(Role.ADMIN))) -> dict:
    logger.debug("config.list_request", operator=_admin.user_id)
    return {"items": await config_service.get_all()}


@router.put("/config", summary="批量更新系统配置（管理端）")
async def update_config(
    body: ConfigUpdateRequest,
    session: AsyncSession = Depends(get_db_session),
    admin: User = Depends(require_roles(Role.ADMIN)),
) -> dict:
    logger.debug("config.update_request", operator=admin.user_id, count=len(body.items))
    try:
        items = await config_service.set_configs(
            session,
            [{"key": i.key, "value": i.value} for i in body.items],
            operator_id=admin.user_id,
        )
    except config_service.ConfigError as e:
        logger.info("config.update_rejected", operator=admin.user_id, error=str(e))
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)) from e
    logger.info("config.update_done", operator=admin.user_id, count=len(body.items))
    return {"items": items}

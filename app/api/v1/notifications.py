"""站内信通知 API（P4.4）。

- GET  /notifications                分页查询（可只看未读）
- GET  /notifications/unread-count   未读数（前端铃铛红点）
- PUT  /notifications/{id}/read      标记单条已读（仅本人）
- PUT  /notifications/read-all       全部已读

权限：任意登录角色（各角色都有通知）。
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_roles
from app.core.database import get_db_session
from app.models.user import Role, User
from app.services import notification_service as svc

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["notifications"])

# 通知查询任一登录角色可用（Role 是常量类，显式列出）
_ANY = (Role.ADMIN, Role.PROJECT_MANAGER, Role.REVIEW_EXPERT, Role.SUPPLIER)


def _note_dict(n) -> dict:
    return {
        "id": n.id,
        "type": n.type,
        "title": n.title,
        "content": n.content,
        "is_read": bool(n.is_read),
        "related_id": n.related_id,
        "created_at": n.created_at.isoformat() if n.created_at else None,
    }


@router.get("/notifications", summary="分页查询通知")
async def list_notifications(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    unread_only: bool = Query(False),
    session: AsyncSession = Depends(get_db_session),
    user: User = Depends(require_roles(*_ANY)),
) -> dict:
    notes = await svc.query(session, user_id=user.user_id, page=page,
                            page_size=page_size, unread_only=unread_only)
    unread = await svc.get_unread_count(session, user.user_id)
    return {"notifications": [_note_dict(n) for n in notes], "unread_count": unread, "page": page}


@router.get("/notifications/unread-count", summary="未读数")
async def unread_count(
    session: AsyncSession = Depends(get_db_session),
    user: User = Depends(require_roles(*_ANY)),
) -> dict:
    count = await svc.get_unread_count(session, user.user_id)
    return {"unread_count": count}


@router.put("/notifications/{notification_id}/read", summary="标记单条已读")
async def mark_read(
    notification_id: int,
    session: AsyncSession = Depends(get_db_session),
    user: User = Depends(require_roles(*_ANY)),
) -> dict:
    ok = await svc.mark_read(session, user_id=user.user_id, notification_id=notification_id)
    if not ok:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="通知不存在或非本人")
    return {"notification_id": notification_id, "is_read": True}


@router.put("/notifications/read-all", summary="全部已读")
async def mark_all_read(
    session: AsyncSession = Depends(get_db_session),
    user: User = Depends(require_roles(*_ANY)),
) -> dict:
    count = await svc.mark_all_read(session, user.user_id)
    return {"updated": count}

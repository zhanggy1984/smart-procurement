"""站内信通知（P4.3 基础 / P4.4 完整）。

send()：写入通知；专家账号映射（display_name=专家名 → user_id）。
P4.4 补 query/mark_read 等 API。
"""

from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import Notification
from app.models.user import User

logger = structlog.get_logger(__name__)


async def send(
    session: AsyncSession,
    *,
    user_id: str,
    type: str,
    title: str,
    content: str | None = None,
    related_id: str | None = None,
) -> Notification:
    """写入一条通知（P4.3 各申报触发点调用）。"""
    note = Notification(
        user_id=user_id, type=type, title=title, content=content, related_id=related_id
    )
    session.add(note)
    await session.commit()
    await session.refresh(note)
    logger.info("notification.send", user_id=user_id, type=type, related_id=related_id)
    return note


async def send_to_expert(
    session: AsyncSession,
    *,
    expert_id: str,
    type: str,
    title: str,
    content: str | None = None,
    related_id: str | None = None,
) -> Notification | None:
    """按专家实体找到其登录账号（display_name=专家名）并通知。返回 None 表示无账号。"""
    from app.models.expert import Expert

    expert = await session.get(Expert, expert_id)
    if expert is None:
        return None
    user = await session.scalar(select(User).where(User.display_name == expert.name))
    if user is None:
        logger.warning("notification.no_account", expert_id=expert_id, name=expert.name)
        return None
    return await send(
        session, user_id=user.user_id, type=type, title=title,
        content=content, related_id=related_id,
    )


# ==================== P4.4：查询 / 已读 ====================

async def query(
    session: AsyncSession,
    *,
    user_id: str,
    page: int = 1,
    page_size: int = 20,
    unread_only: bool = False,
) -> list[Notification]:
    """分页查询通知（新→旧）。unread_only 只看未读。"""
    stmt = select(Notification).where(Notification.user_id == user_id)
    if unread_only:
        stmt = stmt.where(Notification.is_read.is_(False))
    stmt = stmt.order_by(Notification.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    return list((await session.scalars(stmt)).all())


async def get_unread_count(session: AsyncSession, user_id: str) -> int:
    """未读数（前端铃铛红点）。"""
    from sqlalchemy import func

    return int(
        (
            await session.scalar(
                select(func.count(Notification.id)).where(
                    Notification.user_id == user_id, Notification.is_read.is_(False)
                )
            )
        )
        or 0
    )


async def mark_read(session: AsyncSession, *, user_id: str, notification_id: int) -> bool:
    """标记单条已读（仅本人）。返回是否命中。"""
    from sqlalchemy import update

    result = await session.execute(
        update(Notification)
        .where(Notification.id == notification_id, Notification.user_id == user_id)
        .values(is_read=True)
    )
    await session.commit()
    return result.rowcount > 0


async def mark_all_read(session: AsyncSession, user_id: str) -> int:
    """全部已读。返回更新条数。"""
    from sqlalchemy import update

    result = await session.execute(
        update(Notification)
        .where(Notification.user_id == user_id, Notification.is_read.is_(False))
        .values(is_read=True)
    )
    await session.commit()
    return result.rowcount

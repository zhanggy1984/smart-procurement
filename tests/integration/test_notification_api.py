"""P7.3 通知 API 集成测试（task.md #19/#20）。

成功：分页查询 + 未读计数；read-all 未读归零。
错误：未认证 401。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.core.database import session_factory
from app.models.notification import Notification

USER = "ITEST-U-ADMIN"


async def _insert_notifications() -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with session_factory() as s:
        s.add_all([
            Notification(user_id=USER, type="REVIEW_TASK", title="新的评审任务",
                         content="LOT-1 有待评审", is_read=False, created_at=now),
            Notification(user_id=USER, type="SYSTEM", title="历史通知",
                         content="已读", is_read=True, created_at=now),
        ])
        await s.commit()


@pytest.mark.asyncio
async def test_list_notifications_with_unread(client, admin_headers):
    await _insert_notifications()
    resp = await client.get("/api/v1/notifications", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["unread_count"] == 1
    assert len(body["notifications"]) == 2
    assert any(n["is_read"] is False for n in body["notifications"])


@pytest.mark.asyncio
async def test_read_all_zeroes_unread(client, admin_headers):
    await _insert_notifications()
    resp = await client.put("/api/v1/notifications/read-all", headers=admin_headers)
    assert resp.status_code == 200
    # 只更新未读的 1 条（已读的不动）
    assert resp.json()["updated"] == 1
    unread = await client.get("/api/v1/notifications/unread-count", headers=admin_headers)
    assert unread.json()["unread_count"] == 0


@pytest.mark.asyncio
async def test_list_notifications_unauthorized_401(client):
    resp = await client.get("/api/v1/notifications")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_read_all_unauthorized_401(client):
    resp = await client.put("/api/v1/notifications/read-all")
    assert resp.status_code == 401

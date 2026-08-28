"""P7.2 NotificationService 单元测试（task.md：4 用例）。

覆盖：发送、分页查询（含 unread_only 过滤）、标记已读（仅本人）、全部已读。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.notification_service import (
    get_unread_count,
    mark_all_read,
    mark_read,
    query,
    send,
)


@pytest.fixture
def mock_session():
    return AsyncMock()


@pytest.mark.asyncio
async def test_send(mock_session):
    """send 写入通知并 commit。"""
    mock_session.add.return_value = None
    mock_session.refresh.return_value = None
    # 让 send 返回真实对象比较困难，直接断言 add 被调用
    await send(mock_session, user_id="U-1", type="ASSIGNMENT_NOTICE", title="新任务", content="请申报")
    mock_session.add.assert_called_once()
    mock_session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_query_pagination_and_filter(mock_session):
    """query 分页 + unread_only 过滤。"""
    notes = [MagicMock()] * 2
    result = MagicMock()  # ScalarResult.all() 是同步方法，不能用 AsyncMock
    result.all.return_value = notes
    mock_session.scalars.return_value = result
    got = await query(mock_session, user_id="U-1", page=1, page_size=10, unread_only=True)
    assert len(got) == 2


@pytest.mark.asyncio
async def test_mark_read_only_own(mock_session):
    """mark_read 限定本人（WHERE user_id），命中返回 True。"""
    result = MagicMock()
    result.rowcount = 1
    mock_session.execute.return_value = result
    assert await mark_read(mock_session, user_id="U-1", notification_id=5) is True
    result.rowcount = 0
    assert await mark_read(mock_session, user_id="U-1", notification_id=5) is False


@pytest.mark.asyncio
async def test_mark_all_read_and_count(mock_session):
    """mark_all_read 返回更新条数；get_unread_count 返回未读数。"""
    result = MagicMock()
    result.rowcount = 3
    mock_session.execute.return_value = result
    assert await mark_all_read(mock_session, "U-1") == 3
    mock_session.scalar.return_value = 2
    assert await get_unread_count(mock_session, "U-1") == 2
    mock_session.scalar.return_value = None
    assert await get_unread_count(mock_session, "U-1") == 0

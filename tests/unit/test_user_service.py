"""P7.2 UserService 单元测试（task.md：3 用例）。

覆盖：认证成功、密码错误（防枚举，不区分账号不存在）、
create_user 用户名冲突 + update_user 非法角色。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.user_service import (
    InvalidRoleError,
    UsernameTakenError,
    authenticate,
    create_user,
    update_user,
)


@pytest.fixture
def mock_session():
    session = AsyncMock()
    session.get.return_value = None
    return session


@pytest.mark.asyncio
async def test_authenticate_success(mock_session):
    """正确凭据 → 返回 User。"""
    user = MagicMock()
    user.is_active = True
    from app.core import security
    user.password_hash = security.hash_password("Smart@2026")
    # get_user_by_username 返回 user
    result = MagicMock()
    result.scalar_one_or_none.return_value = user
    mock_session.execute.return_value = result
    got = await authenticate(mock_session, "pm1", "Smart@2026")
    assert got is user


@pytest.mark.asyncio
async def test_authenticate_wrong_password_returns_none(mock_session):
    """密码错误 → None（不区分账号不存在，防枚举）。"""
    from app.core import security
    user = MagicMock()
    user.is_active = True
    user.password_hash = security.hash_password("Other@123")
    result = MagicMock()
    result.scalar_one_or_none.return_value = user
    mock_session.execute.return_value = result
    got = await authenticate(mock_session, "pm1", "Wrong@999")
    assert got is None


@pytest.mark.asyncio
async def test_authenticate_inactive_returns_none(mock_session):
    """停用账号即使密码正确 → None。"""
    from app.core import security
    user = MagicMock()
    user.is_active = False
    user.password_hash = security.hash_password("Smart@2026")
    result = MagicMock()
    result.scalar_one_or_none.return_value = user
    mock_session.execute.return_value = result
    assert await authenticate(mock_session, "suspended", "Smart@2026") is None


@pytest.mark.asyncio
async def test_create_user_username_taken(mock_session):
    """用户名已存在 → UsernameTakenError。"""
    existing = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = existing
    mock_session.execute.return_value = result
    with pytest.raises(UsernameTakenError):
        await create_user(mock_session, username="pm1", password="Smart@2026", display_name="已存在")


@pytest.mark.asyncio
async def test_update_user_invalid_role(mock_session):
    """update_user 非法角色 → InvalidRoleError。"""
    user = MagicMock()
    mock_session.get.return_value = user
    with pytest.raises(InvalidRoleError):
        await update_user(mock_session, "U-1", role="NOT_A_ROLE")


@pytest.mark.asyncio
async def test_create_user_success(mock_session):
    """合法密码 + 用户名未占用 → 创建并 commit。"""
    result = MagicMock()
    result.scalar_one_or_none.return_value = None  # 用户名查重：未占用
    mock_session.execute.return_value = result
    created = await create_user(mock_session, username="pm_new", password="Smart@2026",
                                display_name="新PM", role="PROJECT_MANAGER")
    assert created.username == "pm_new"
    assert created.role == "PROJECT_MANAGER"
    mock_session.add.assert_called_once()
    mock_session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_user_weak_password_rejected(mock_session):
    """密码不满足复杂度 → 抛错（真实强度校验，不落库）。"""
    with pytest.raises(Exception, match=".*密码.*"):
        await create_user(mock_session, username="pm_weak", password="123",
                          display_name="弱密码")
    mock_session.add.assert_not_called()


@pytest.mark.asyncio
async def test_update_user_success():
    """更新启用状态 + 角色 → 返回更新后的 user。"""
    session = AsyncMock()
    user = MagicMock()
    user.is_active = True
    session.get.return_value = user
    done = await update_user(session, "U-1", is_active=False, role="REVIEW_EXPERT")
    assert done is user
    assert user.is_active is False
    assert user.role == "REVIEW_EXPERT"
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_user_not_found():
    """用户不存在 → None（不抛错）。"""
    session = AsyncMock()
    session.get.return_value = None
    assert await update_user(session, "U-X", is_active=False) is None


@pytest.mark.asyncio
async def test_list_users_keyword():
    """用户列表：keyword 过滤 + 分页。"""
    from app.services.user_service import list_users

    session = AsyncMock()
    count_result = MagicMock()
    count_result.scalar_one.return_value = 1
    rows_scalar = MagicMock()
    rows_scalar.all.return_value = [MagicMock(username="pm1")]
    rows_result = MagicMock()
    rows_result.scalars.return_value = rows_scalar
    session.execute.side_effect = [count_result, rows_result]
    items, total = await list_users(session, page=1, page_size=20, keyword="pm")
    assert total == 1
    assert len(items) == 1


# ==================== 自查 #6：改密（change_password） ====================


@pytest.mark.asyncio
async def test_change_password_wrong_old_password():
    """旧密码错误 → InvalidOldPasswordError，不提交。"""
    from app.core import security
    from app.services.user_service import InvalidOldPasswordError, change_password

    session = AsyncMock()
    user = MagicMock()
    user.password_hash = security.hash_password("Smart@2026")
    with pytest.raises(InvalidOldPasswordError):
        await change_password(session, user, old_password="Wrong@999", new_password="New@Pass123")
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_change_password_weak_new_password():
    """新密码不满足复杂度 → PasswordStrengthError，不提交。"""
    from app.core import security
    from app.services.user_service import change_password

    session = AsyncMock()
    user = MagicMock()
    user.password_hash = security.hash_password("Smart@2026")
    with pytest.raises(security.PasswordStrengthError):
        await change_password(session, user, old_password="Smart@2026", new_password="123")
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_change_password_success():
    """旧密码正确 + 新密码合规 → 哈希更新 + 清首登强改标记 + 提交。"""
    from app.core import security
    from app.services.user_service import change_password

    session = AsyncMock()
    user = MagicMock()
    user.password_hash = security.hash_password("Smart@2026")
    await change_password(session, user, old_password="Smart@2026", new_password="New@Pass123")
    assert user.must_change_password is False
    assert security.verify_password("New@Pass123", user.password_hash)
    session.commit.assert_awaited_once()

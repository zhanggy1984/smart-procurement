"""用户认证服务（P1.2）。

职责：create_user（含密码复杂度 + bcrypt）、authenticate（登录校验）、
get_user（按 id / username 查询）。JWT 签发在 auth API 层组装。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import security
from app.core.crypto import generate_id
from app.models.user import Role, User


class UsernameTakenError(ValueError):
    """用户名已存在。"""


class InvalidRoleError(ValueError):
    """角色非法（非 Role.ALL 受控值）。"""


async def create_user(
    session: AsyncSession,
    *,
    username: str,
    password: str,
    role: str = Role.REVIEW_EXPERT,
    display_name: str,
    email: Optional[str] = None,
    phone: Optional[str] = None,
) -> User:
    """创建用户。密码先做复杂度校验再 bcrypt，用户名冲突抛 UsernameTakenError。"""
    security.validate_password_strength(password)
    existing = await get_user_by_username(session, username)
    if existing is not None:
        raise UsernameTakenError(f"用户名已存在: {username}")

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    user = User(
        user_id=generate_id("U"),
        username=username,
        password_hash=security.hash_password(password),
        role=role,
        display_name=display_name,
        email=email,
        phone=phone,
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def authenticate(session: AsyncSession, username: str, password: str) -> Optional[User]:
    """用户名+密码校验。成功返回 User，失败返回 None（不区分账号不存在/密码错误，防枚举）。"""
    user = await get_user_by_username(session, username)
    if user is None or not security.verify_password(password, user.password_hash):
        return None
    if not user.is_active:
        return None
    return user


async def get_user(session: AsyncSession, user_id: str) -> Optional[User]:
    """按 user_id 查询。"""
    return await session.get(User, user_id)


async def get_user_by_username(session: AsyncSession, username: str) -> Optional[User]:
    """按 username 查询。"""
    result = await session.execute(select(User).where(User.username == username))
    return result.scalar_one_or_none()


async def list_users(
    session: AsyncSession,
    *,
    page: int = 1,
    page_size: int = 20,
    keyword: Optional[str] = None,
) -> tuple[list[User], int]:
    """分页查询用户（P6.2 用户管理页）。keyword 模糊匹配 username/display_name。"""
    stmt = select(User)
    if keyword:
        like = f"%{keyword}%"
        stmt = stmt.where((User.username.like(like)) | (User.display_name.like(like)))
    total = (
        await session.execute(select(func.count()).select_from(stmt.subquery()))
    ).scalar_one()
    rows = (
        await session.execute(
            stmt.order_by(User.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()
    return list(rows), total


async def update_user(
    session: AsyncSession,
    user_id: str,
    *,
    is_active: Optional[bool] = None,
    role: Optional[str] = None,
) -> Optional[User]:
    """更新用户启停状态 / 角色（P6.2）。返回 None 表示用户不存在。"""
    user = await session.get(User, user_id)
    if user is None:
        return None
    if role is not None:
        if role not in Role.ALL:
            raise InvalidRoleError(f"角色非法: {role}")
        user.role = role
    if is_active is not None:
        user.is_active = is_active
    user.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    await session.commit()
    await session.refresh(user)
    return user

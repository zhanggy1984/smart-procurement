"""用户认证模型（P1.2）。

对应 P0.4 migration 的 users 表。role 为 VARCHAR(16)，用常量类约束取值，
避免误写拼写错误；不引入 DB 枚举（表结构已定，改动需 migration）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Role:
    """用户角色受控值（solution.md users 表 DDL）。"""

    ADMIN = "ADMIN"
    PROJECT_MANAGER = "PROJECT_MANAGER"
    REVIEW_EXPERT = "REVIEW_EXPERT"
    SUPPLIER = "SUPPLIER"

    ALL = (ADMIN, PROJECT_MANAGER, REVIEW_EXPERT, SUPPLIER)


class User(Base):
    """登录账号。password_hash 存 bcrypt，明文密码不落库。"""

    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, server_default=Role.REVIEW_EXPERT)
    display_name: Mapped[str] = mapped_column(String(64), nullable=False)
    email: Mapped[Optional[str]] = mapped_column(String(128))
    phone: Mapped[Optional[str]] = mapped_column(String(20))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("TRUE"))
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

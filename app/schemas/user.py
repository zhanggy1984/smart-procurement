"""用户管理 schema（P6.2 管理员端用户管理页）。"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict

from app.models.user import Role


class UserOut(BaseModel):
    """用户响应（不含 password_hash）。"""

    model_config = ConfigDict(from_attributes=True)

    user_id: str
    username: str
    role: str
    display_name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    is_active: bool
    must_change_password: bool = False
    created_at: Optional[datetime] = None


class UserCreate(BaseModel):
    """创建用户请求。密码复杂度由 service 校验。"""

    username: str
    password: str
    role: str = Role.REVIEW_EXPERT
    display_name: str
    email: Optional[str] = None
    phone: Optional[str] = None


class UserStatusUpdate(BaseModel):
    """启用/禁用/改角色。至少传一个字段。"""

    is_active: Optional[bool] = None
    role: Optional[str] = None

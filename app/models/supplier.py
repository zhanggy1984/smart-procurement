"""供应商域模型（P1.4）：supplier。

对应 P0.4 migration 建的表。blacklisted 布尔与 status 并存：
- 拉黑：blacklisted=True 且 status=INACTIVE（与合成数据 SUP-005 语义一致）
- 正常：blacklisted=False 且 status=ACTIVE
供应商登录账号与实体通过 users.username 前缀约定关联（supplier 表无 user_id 列，
DDL 已定，不引入 migration 改动）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class SupplierStatus:
    """供应商状态受控值。拉黑状态由 blacklisted 布尔表达，status 保持二元。"""

    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"

    ALL = (ACTIVE, INACTIVE)


class Supplier(Base):
    """供应商（投标主体）。"""

    __tablename__ = "supplier"

    supplier_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    uniform_credit_code: Mapped[Optional[str]] = mapped_column(String(32))
    legal_person: Mapped[Optional[str]] = mapped_column(String(64))
    industry: Mapped[Optional[str]] = mapped_column(String(64))
    scale: Mapped[Optional[str]] = mapped_column(String(16))
    blacklisted: Mapped[bool] = mapped_column(Boolean, server_default=text("FALSE"))
    status: Mapped[str] = mapped_column(String(16), server_default=SupplierStatus.ACTIVE)
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

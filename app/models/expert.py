"""专家域模型（P1.4）：expert / expert_specialization。

对应 P0.4 migration 建的表。身份证只存加密+哈希（明文不落库），
加密/哈希由 core/crypto.py 统一处理。状态三态 ACTIVE/INACTIVE/BLACKLISTED
（solution.md Expert DDL），BLACKLISTED/INACTIVE 专家的登录账号由
service 同步禁用（users.is_active=False）。

注意：不定义 relationship（逻辑外键 + async 懒加载抛 MissingGreenlet），
关联数据一律 service 显式查询组装。
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ExpertStatus:
    """专家状态受控值（solution.md Expert DDL）。"""

    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    BLACKLISTED = "BLACKLISTED"

    ALL = (ACTIVE, INACTIVE, BLACKLISTED)


class Expert(Base):
    """评审专家。user_id 关联登录账号（唯一）。"""

    __tablename__ = "expert"

    expert_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[Optional[str]] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    organization: Mapped[Optional[str]] = mapped_column(String(128))
    region: Mapped[Optional[str]] = mapped_column(String(32))
    experience: Mapped[Optional[int]] = mapped_column(Integer)
    email: Mapped[Optional[str]] = mapped_column(String(128))
    phone: Mapped[Optional[str]] = mapped_column(String(20))
    id_number_encrypted: Mapped[Optional[str]] = mapped_column(String(256))
    id_number_hash: Mapped[Optional[str]] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), server_default=ExpertStatus.ACTIVE)
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class ExpertSpecialization(Base):
    """专家专业标签（一对多，复合主键）。"""

    __tablename__ = "expert_specialization"

    expert_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tag: Mapped[str] = mapped_column(String(64), primary_key=True)

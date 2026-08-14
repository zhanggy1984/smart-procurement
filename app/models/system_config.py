"""系统配置（P6.2）。对应 P0.4 migration 中第 19 张表 system_config。

管理员在配置页修改业务参数（LLM/回避/围串标阈值），写入本表 + 内存缓存，
运行时即时生效（无需重启）。与 .env 基础设施配置层解耦。
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class SystemConfig(Base):
    """运行时系统配置项。config_key 为业务键（如 fraud.critical_threshold）。"""

    __tablename__ = "system_config"

    config_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    config_value: Mapped[str] = mapped_column(String(256), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(512))
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, server_default=text("NOW()"))
    updated_by: Mapped[Optional[str]] = mapped_column(String(64))

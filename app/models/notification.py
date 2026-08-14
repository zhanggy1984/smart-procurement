"""站内信通知（P4.3 基础 / P4.4 完整）。对应 P0.4 migration。"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, Boolean, DateTime, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Notification(Base):
    """站内信。type 为业务事件（回避申报/评审分配/偏差告警等 9 种）。"""

    __tablename__ = "notification"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    content: Mapped[Optional[str]] = mapped_column(Text)
    is_read: Mapped[bool] = mapped_column(Boolean, server_default=text("FALSE"))
    related_id: Mapped[Optional[str]] = mapped_column(String(64))
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime, server_default=text("NOW()"))

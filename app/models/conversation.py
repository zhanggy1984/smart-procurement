"""评审对话消息模型（P2.3）。

对应 P0.4 migration 的 conversation_message 表。多轮对话按 review+dimension 组织：
- turn_number：review 内全局递增（跨维度共享序号）
- dim_turn_number：同 review+dimension 内递增（维度内追问序号）
- message_type：MESSAGE（正常对话）/ SUMMARY（DeepSeek 压缩的阶段摘要）
- citations / score_suggestion：AI 评审响应携带的证据与建议（P3.x 填充）
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, DateTime, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class MessageType:
    """对话消息类型。SUMMARY 由 maybe_summarize 在第 4 轮触发写入。"""

    MESSAGE = "MESSAGE"
    SUMMARY = "SUMMARY"


class ConversationMessage(Base):
    """评审工作台多轮对话消息。"""

    __tablename__ = "conversation_message"

    message_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    review_id: Mapped[str] = mapped_column(String(64), nullable=False)
    dimension_id: Mapped[Optional[str]] = mapped_column(String(64))
    turn_number: Mapped[int] = mapped_column(Integer, nullable=False)
    dim_turn_number: Mapped[int] = mapped_column(Integer, server_default="0")
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # user / assistant / system
    message_type: Mapped[str] = mapped_column(String(16), server_default="MESSAGE")
    intent: Mapped[Optional[str]] = mapped_column(String(32))
    content: Mapped[Optional[str]] = mapped_column(Text)
    citations: Mapped[Optional[dict]] = mapped_column(JSON)
    score_suggestion: Mapped[Optional[dict]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), server_default="COMPLETE")
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime, server_default=text("NOW()"))

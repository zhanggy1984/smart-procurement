"""评审工作台 schema（P3.3）。"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class ReviewCreate(BaseModel):
    """创建评审工作台：标书 + 评分维度。"""

    bid_id: str = Field(..., description="标书 ID（需 FROZEN）")
    dimension_id: str = Field(..., description="评分维度 ID（归属该标段）")


class ReviewOut(BaseModel):
    """评审记录响应。"""

    model_config = ConfigDict(from_attributes=True)

    review_id: str
    expert_id: str
    bid_id: str
    dimension_id: str
    score: Optional[Decimal] = None
    comment: Optional[str] = None
    ai_suggestion: Optional[dict] = None
    status: str


class ChatRequest(BaseModel):
    """对话请求。"""

    question: str = Field(..., description="追问内容")


class SaveScoreRequest(BaseModel):
    """评分暂存。"""

    score: float = Field(..., description="专家分数（0~maxScore）")
    comment: str = Field("", description="评分理由/评语")
    ai_suggestion: Optional[dict] = None

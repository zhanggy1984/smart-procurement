"""投标文件请求/响应 schema（P1.5）。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict


class BidUploadResult(BaseModel):
    """上传成功响应：记录 + 30min 预签名下载 URL。"""

    bid_id: str
    lot_id: str
    supplier_id: str
    filename: str
    status: str
    parsing_step: int
    file_url: str
    presigned_url: str


class BidOut(BaseModel):
    """标书详情（含结构化数据 + 动态生成的预签名 URL）。"""

    model_config = ConfigDict(from_attributes=True)

    bid_id: str
    lot_id: str
    supplier_id: str
    bid_amount: Optional[Decimal] = None
    duration: Optional[int] = None
    team_size: Optional[int] = None
    structured_data: Optional[dict] = None
    file_url: Optional[str] = None
    status: str
    parsing_step: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    presigned_url: Optional[str] = None  # 动态生成，非 DB 列


class BidStatusOut(BaseModel):
    """解析进度（含 checkpoint）。"""

    model_config = ConfigDict(from_attributes=True)

    bid_id: str
    status: str
    parsing_step: Optional[int] = None
    updated_at: Optional[datetime] = None

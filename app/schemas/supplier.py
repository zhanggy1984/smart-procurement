"""供应商域请求/响应 schema（P1.4）。

拉黑/恢复/逻辑删除通过 SupplierStatusUpdate 的 blacklisted + status 组合表达：
- {"blacklisted": true}  → 拉黑（status→INACTIVE + 黑名单级联）
- {"blacklisted": false} → 解除拉黑（status→ACTIVE + 评审按 previous_status 恢复）
- {"status": "INACTIVE"} → 逻辑删除（不触发级联）
- {"status": "ACTIVE"}   → 恢复启用
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict


class SupplierStatusUpdate(BaseModel):
    """供应商状态变更（拉黑/解除/停用/启用）。"""

    status: Optional[Literal["ACTIVE", "INACTIVE"]] = None
    blacklisted: Optional[bool] = None


class SupplierImportResult(BaseModel):
    """Excel 批量导入结果。"""

    imported: int
    skipped: int  # uniform_credit_code 已存在被去重跳过


class SupplierOut(BaseModel):
    """供应商详情响应。"""

    model_config = ConfigDict(from_attributes=True)

    supplier_id: str
    name: str
    uniform_credit_code: Optional[str] = None
    legal_person: Optional[str] = None
    industry: Optional[str] = None
    scale: Optional[str] = None
    blacklisted: bool
    status: str

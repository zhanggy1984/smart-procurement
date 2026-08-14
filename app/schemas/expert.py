"""专家域请求/响应 schema（P1.4）。

导入的字段级校验（region/tag 受控值、身份证格式）在 service 层逐行收集错误
（整批一个事务 + 422 带行号），此处只定义响应与状态变更结构。
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict

# 专家状态受控值（solution.md Expert DDL 三态）
_EXPERT_STATUS = Literal["ACTIVE", "INACTIVE", "BLACKLISTED"]


class ExpertStatusUpdate(BaseModel):
    """启用/停用/拉黑。INACTIVE/BLACKLISTED 同步禁用登录账号。"""

    status: _EXPERT_STATUS


class ExpertImportResult(BaseModel):
    """Excel 批量导入结果。"""

    imported: int
    skipped: int  # id_number_hash 已存在被去重跳过


class ExpertOut(BaseModel):
    """专家详情响应。tags 由 service 组装（无 relationship 懒加载）。"""

    model_config = ConfigDict(from_attributes=True)

    expert_id: str
    name: str
    organization: Optional[str] = None
    region: Optional[str] = None
    experience: Optional[int] = None
    status: str
    user_id: Optional[str] = None
    tags: list[str] = []

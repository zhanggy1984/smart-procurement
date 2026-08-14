"""项目域请求/响应 schema（P1.3）。

校验点（solution.md 4 核心 API）：
- region 受控值（constants.REGIONS）
- SUM(lot.budget) ≤ project.budget
- SUM(weight)=1.0±0.001、SUM(maxScore)=100
- expert_count ≥ min_experts_per_dimension
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.constants import PROJECT_TYPES, REGIONS


# ==================== 项目 ====================
class ProjectCreate(BaseModel):
    """创建项目。region 校验受控值，status 默认 DRAFT。"""

    project_code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=256)
    type: str
    region: Optional[str] = None
    budget: Decimal = Field(gt=0, decimal_places=2)
    managed_by: Optional[str] = None

    @model_validator(mode="after")
    def _validate_controlled_values(self) -> "ProjectCreate":
        if self.type not in PROJECT_TYPES:
            raise ValueError(f"type 非法，必须是 {PROJECT_TYPES}")
        if self.region is not None and self.region not in REGIONS:
            raise ValueError(f"region 非法，必须是受控值: {REGIONS}")
        return self


class LotItem(BaseModel):
    """项目详情里的标段。"""

    model_config = ConfigDict(from_attributes=True)

    lot_id: str
    lot_code: str
    name: str
    budget: Optional[Decimal] = None
    status: str


class ProjectOut(BaseModel):
    """项目详情响应。"""

    model_config = ConfigDict(from_attributes=True)

    project_id: str
    project_code: str
    name: str
    type: str
    region: Optional[str] = None
    budget: Optional[Decimal] = None
    status: str
    managed_by: Optional[str] = None
    created_at: Optional[datetime] = None
    lots: list[LotItem] = []


# ==================== 标段 ====================
class LotCreate(BaseModel):
    """创建标段。SUM(lot.budget) ≤ project.budget 由 service 校验。"""

    lot_code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=256)
    budget: Decimal = Field(gt=0, decimal_places=2)


class LotOut(BaseModel):
    """标段详情响应。"""

    model_config = ConfigDict(from_attributes=True)

    lot_id: str
    project_id: str
    lot_code: str
    name: str
    budget: Optional[Decimal] = None
    status: str
    created_at: Optional[datetime] = None


# ==================== 评分维度 ====================
class CriterionCreate(BaseModel):
    """评分标准子项。"""

    name: str = Field(min_length=1, max_length=128)
    description: Optional[str] = None
    scoring_rubric: Optional[str] = None
    max_score: Decimal = Field(gt=0, decimal_places=2)


class DimensionCreate(BaseModel):
    """配置单个评分维度。权重和校验在 service 层（跨维度）。"""

    name: str = Field(min_length=1, max_length=64)
    max_score: Decimal = Field(gt=0, decimal_places=2)
    weight: Decimal = Field(gt=0, decimal_places=3)
    sort_order: Optional[int] = 0
    criteria: list[CriterionCreate] = []


class DimensionsCreateRequest(BaseModel):
    """批量配置标段维度（覆盖式写入）。"""

    dimensions: list[DimensionCreate] = Field(min_length=1, max_length=10)


class DimensionOut(BaseModel):
    """维度响应。"""

    model_config = ConfigDict(from_attributes=True)

    dimension_id: str
    lot_id: str
    name: str
    max_score: Decimal
    weight: Decimal
    sort_order: int


# ==================== 专家遴选配置 ====================
class ExpertCriteriaCreate(BaseModel):
    """配置专家遴选参数。权重和 = 1.0、expert_count ≥ min 由 service 校验。"""

    expert_count: int = Field(ge=1, le=30)
    min_experts_per_dimension: int = Field(ge=1, le=10)
    weight_specialization: Decimal = Field(gt=0, decimal_places=3)
    weight_experience: Decimal = Field(gt=0, decimal_places=3)
    weight_review_quality: Decimal = Field(gt=0, decimal_places=3)
    weight_region: Decimal = Field(gt=0, decimal_places=3)
    min_experience: int = Field(ge=0, le=40)


class ExpertCriteriaOut(BaseModel):
    """遴选配置响应。"""

    model_config = ConfigDict(from_attributes=True)

    lot_id: str
    expert_count: int
    min_experts_per_dimension: int
    weight_specialization: Decimal
    weight_experience: Decimal
    weight_review_quality: Decimal
    weight_region: Decimal
    min_experience: int

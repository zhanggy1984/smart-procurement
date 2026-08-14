"""项目域模型（P1.3）：project / lot / scoring_dimension / scoring_criterion / lot_expert_criteria。

对应 P0.4 migration 建的表。预算/分值为 Numeric（避免浮点误差），
权重为 Numeric(4,3)（3 位小数，校验 SUM=1.0±0.001 用）。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import DateTime, Integer, JSON, Numeric, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Project(Base):
    """采购项目。"""

    __tablename__ = "project"

    project_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    region: Mapped[Optional[str]] = mapped_column(String(32))
    budget: Mapped[Optional[Decimal]] = mapped_column(Numeric(15, 2))
    status: Mapped[str] = mapped_column(String(32), server_default="DRAFT")
    managed_by: Mapped[Optional[str]] = mapped_column(String(64))
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    # 注意：不定义 relationship。表为逻辑外键（无 DB FK 约束），
    # 且 async session 下 relationship 懒加载抛 MissingGreenlet，
    # 关联数据一律由 service 显式查询组装。


class Lot(Base):
    """标段。"""

    __tablename__ = "lot"

    lot_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    lot_code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    budget: Mapped[Optional[Decimal]] = mapped_column(Numeric(15, 2))
    status: Mapped[str] = mapped_column(String(32), server_default="BIDDING")
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class ScoringDimension(Base):
    """评分维度（权重和 = 1.0 由 service 校验）。"""

    __tablename__ = "scoring_dimension"

    dimension_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    lot_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    max_score: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 2), nullable=False)
    weight: Mapped[Optional[Decimal]] = mapped_column(Numeric(4, 3), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, server_default="0")
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class ScoringCriterion(Base):
    """评分标准子项（含打分标尺）。"""

    __tablename__ = "scoring_criterion"

    criterion_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    dimension_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    scoring_rubric: Mapped[Optional[str]] = mapped_column(Text)
    max_score: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 2), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, server_default="0")


class LotExpertCriteria(Base):
    """标段专家遴选配置（P4.2 匹配算法读取）。"""

    __tablename__ = "lot_expert_criteria"

    lot_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    expert_count: Mapped[int] = mapped_column(Integer, server_default="5")
    min_experts_per_dimension: Mapped[int] = mapped_column(Integer, server_default="2")
    weight_specialization: Mapped[Decimal] = mapped_column(Numeric(4, 3), server_default="0.40")
    weight_experience: Mapped[Decimal] = mapped_column(Numeric(4, 3), server_default="0.30")
    weight_review_quality: Mapped[Decimal] = mapped_column(Numeric(4, 3), server_default="0.20")
    weight_region: Mapped[Decimal] = mapped_column(Numeric(4, 3), server_default="0.10")
    min_experience: Mapped[int] = mapped_column(Integer, server_default="5")
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

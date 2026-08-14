"""ORM 模型包。模型以 users 表为起点（P1.2），P1.3+ 逐步补充。

引入时保证 Base.metadata 已包含全部模型，供 Alembic autogenerate 使用
（当前 migration 为手写 DDL，autogenerate 仅作辅助）。
"""

from app.models.base import Base
from app.models.outbox import OutboxEvent, OutboxEventType
from app.models.system_config import SystemConfig
from app.models.project import (
    Lot,
    LotExpertCriteria,
    Project,
    ScoringCriterion,
    ScoringDimension,
)
from app.models.user import Role, User

__all__ = [
    "Base",
    "Role",
    "User",
    "Project",
    "Lot",
    "ScoringDimension",
    "ScoringCriterion",
    "LotExpertCriteria",
    "OutboxEvent",
    "OutboxEventType",
    "SystemConfig",
]

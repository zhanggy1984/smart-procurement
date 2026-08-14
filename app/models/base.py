"""SQLAlchemy 声明式 Base —— 所有 ORM 模型继承于此。

表结构已由 P0.4 Alembic migration 创建（`alembic upgrade head`），
模型仅声明映射，不做 autogenerate（结构变更走 migration）。
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """项目统一 ORM 基类。"""

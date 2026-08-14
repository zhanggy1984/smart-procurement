"""专家专业标签模型 — 定义见 app/models/expert.py（ExpertSpecialization）。

本文件为独立 import 入口（re-export），避免与 expert.py 重复定义同名表
（SQLAlchemy MetaData 会拒绝重复 __tablename__）。
"""

from app.models.expert import ExpertSpecialization

__all__ = ["ExpertSpecialization"]

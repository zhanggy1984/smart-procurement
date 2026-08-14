"""create all 22 tables

P0.4 第 1 个 migration：按 solution.md 的 DDL 创建全部 22 张 MySQL 表。
表结构以 solution.md 为唯一权威来源（task.md 称 21 张，实为 22 张，含 audit_log）。
外键均为逻辑外键（VARCHAR 存 ID，不建 DB 级 FOREIGN KEY），建表顺序无关紧要。

Revision ID: ecccf99884f8
Revises:
Create Date: 2026-08-11 22:59:50.191920

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ecccf99884f8'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """创建全部 22 张表 + 二级索引。"""
    # ==================== 1. users 用户认证 ====================
    op.create_table(
        "users",
        sa.Column("user_id", sa.String(64), primary_key=True),
        sa.Column("username", sa.String(64), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(256), nullable=False),
        sa.Column("role", sa.String(16), nullable=False, server_default="REVIEW_EXPERT"),
        sa.Column("display_name", sa.String(64), nullable=False),
        sa.Column("email", sa.String(128)),
        sa.Column("phone", sa.String(20)),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("TRUE")),
        sa.Column("created_at", sa.DateTime()),
        sa.Column("updated_at", sa.DateTime()),
    )

    # ==================== 2. expert 专家 ====================
    op.create_table(
        "expert",
        sa.Column("expert_id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(64), unique=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("organization", sa.String(128)),
        sa.Column("region", sa.String(32)),
        sa.Column("experience", sa.Integer()),
        sa.Column("email", sa.String(128)),
        sa.Column("phone", sa.String(20)),
        sa.Column("id_number_encrypted", sa.String(256)),
        sa.Column("id_number_hash", sa.String(64)),
        sa.Column("status", sa.String(16), server_default="ACTIVE"),
        sa.Column("created_at", sa.DateTime()),
        sa.Column("updated_at", sa.DateTime()),
    )

    # ==================== 3. expert_specialization 专家专业标签 ====================
    op.create_table(
        "expert_specialization",
        sa.Column("expert_id", sa.String(64), primary_key=True),
        sa.Column("tag", sa.String(64), primary_key=True),
    )

    # ==================== 4. bid_document 投标文件 ====================
    op.create_table(
        "bid_document",
        sa.Column("bid_id", sa.String(64), primary_key=True),
        sa.Column("lot_id", sa.String(64), nullable=False),
        sa.Column("supplier_id", sa.String(64), nullable=False),
        sa.Column("bid_amount", sa.Numeric(15, 2)),
        sa.Column("duration", sa.Integer()),
        sa.Column("team_size", sa.Integer()),
        sa.Column("structured_data", sa.JSON()),
        sa.Column("file_url", sa.String(512)),
        sa.Column("status", sa.String(16), server_default="SUBMITTED"),
        sa.Column("freeze_hash", sa.String(128)),
        sa.Column("parsing_step", sa.SmallInteger(), server_default="0"),
        sa.Column("created_at", sa.DateTime()),
        sa.Column("updated_at", sa.DateTime()),
    )

    # ==================== 5. expert_review 评审记录 ====================
    op.create_table(
        "expert_review",
        sa.Column("review_id", sa.String(64), primary_key=True),
        sa.Column("expert_id", sa.String(64), nullable=False),
        sa.Column("bid_id", sa.String(64), nullable=False),
        sa.Column("dimension_id", sa.String(64), nullable=False),
        sa.Column("score", sa.Numeric(5, 2)),
        sa.Column("comment", sa.Text()),
        sa.Column("ai_suggestion", sa.JSON()),
        sa.Column("status", sa.String(16), server_default="DRAFT"),
        sa.Column("previous_status", sa.String(16)),
        sa.Column("created_at", sa.DateTime()),
        sa.Column("updated_at", sa.DateTime()),
    )

    # ==================== 6. supplier 供应商 ====================
    op.create_table(
        "supplier",
        sa.Column("supplier_id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("uniform_credit_code", sa.String(32)),
        sa.Column("legal_person", sa.String(64)),
        sa.Column("industry", sa.String(64)),
        sa.Column("scale", sa.String(16)),
        sa.Column("blacklisted", sa.Boolean(), server_default=sa.text("FALSE")),
        sa.Column("status", sa.String(16), server_default="ACTIVE"),
        sa.Column("created_at", sa.DateTime()),
        sa.Column("updated_at", sa.DateTime()),
    )

    # ==================== 7. project 采购项目 ====================
    op.create_table(
        "project",
        sa.Column("project_id", sa.String(64), primary_key=True),
        sa.Column("project_code", sa.String(32), nullable=False, unique=True),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("type", sa.String(16), nullable=False),
        sa.Column("region", sa.String(32)),
        sa.Column("budget", sa.Numeric(15, 2)),
        sa.Column("status", sa.String(32), server_default="DRAFT"),
        sa.Column("managed_by", sa.String(64)),
        sa.Column("created_at", sa.DateTime()),
        sa.Column("updated_at", sa.DateTime()),
    )

    # ==================== 8. lot 标段 ====================
    op.create_table(
        "lot",
        sa.Column("lot_id", sa.String(64), primary_key=True),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("lot_code", sa.String(32), nullable=False),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("budget", sa.Numeric(15, 2)),
        sa.Column("status", sa.String(32), server_default="BIDDING"),
        sa.Column("created_at", sa.DateTime()),
        sa.Column("updated_at", sa.DateTime()),
    )

    # ==================== 9. scoring_dimension 评分维度 ====================
    op.create_table(
        "scoring_dimension",
        sa.Column("dimension_id", sa.String(64), primary_key=True),
        sa.Column("lot_id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("max_score", sa.Numeric(5, 2), nullable=False),
        sa.Column("weight", sa.Numeric(4, 3), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default="0"),
        sa.Column("created_at", sa.DateTime()),
    )

    # ==================== 10. scoring_criterion 评分标准子项 ====================
    op.create_table(
        "scoring_criterion",
        sa.Column("criterion_id", sa.String(64), primary_key=True),
        sa.Column("dimension_id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("scoring_rubric", sa.Text()),
        sa.Column("max_score", sa.Numeric(5, 2), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default="0"),
    )

    # ==================== 11. lot_expert_criteria 标段专家遴选配置 ====================
    op.create_table(
        "lot_expert_criteria",
        sa.Column("lot_id", sa.String(64), primary_key=True),
        sa.Column("expert_count", sa.Integer(), server_default="5"),
        sa.Column("min_experts_per_dimension", sa.Integer(), server_default="2"),
        sa.Column("weight_specialization", sa.Numeric(4, 3), server_default="0.40"),
        sa.Column("weight_experience", sa.Numeric(4, 3), server_default="0.30"),
        sa.Column("weight_review_quality", sa.Numeric(4, 3), server_default="0.20"),
        sa.Column("weight_region", sa.Numeric(4, 3), server_default="0.10"),
        sa.Column("min_experience", sa.Integer(), server_default="5"),
        sa.Column("created_at", sa.DateTime()),
        sa.Column("updated_at", sa.DateTime()),
    )

    # ==================== 12. pending_conflict 企查查冷数据 ====================
    op.create_table(
        "pending_conflict",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("person_name", sa.String(64)),
        sa.Column("company_name", sa.String(256)),
        sa.Column("credit_code", sa.String(32)),
        sa.Column("relation_type", sa.String(32)),
        sa.Column("expert_id", sa.String(64)),
        sa.Column("supplier_id", sa.String(64)),
        sa.Column("status", sa.String(16), server_default="PENDING"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("NOW()")),
    )

    # ==================== 13. lot_expert_assignment 专家-标段分配 ====================
    op.create_table(
        "lot_expert_assignment",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("lot_id", sa.String(64), nullable=False),
        sa.Column("expert_id", sa.String(64), nullable=False),
        sa.Column("dimension_ids", sa.JSON()),
        sa.Column("match_batch_id", sa.String(64)),
        sa.Column("assigned_at", sa.DateTime(), server_default=sa.text("NOW()")),
        sa.Column("status", sa.String(32), server_default="PENDING_DECLARATION"),
        sa.UniqueConstraint("lot_id", "expert_id", name="uq_lot_expert"),
    )

    # ==================== 14. expert_conflict_declaration 专家回避申报 ====================
    op.create_table(
        "expert_conflict_declaration",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("assignment_id", sa.BigInteger(), nullable=False),
        sa.Column("expert_id", sa.String(64), nullable=False),
        sa.Column("lot_id", sa.String(64), nullable=False),
        sa.Column("supplier_id", sa.String(64)),
        sa.Column("relation_type", sa.String(32), nullable=False),
        sa.Column("relation_detail", sa.Text()),
        sa.Column("declared_at", sa.DateTime(), server_default=sa.text("NOW()")),
    )

    # ==================== 15. conversation_message 对话消息 ====================
    op.create_table(
        "conversation_message",
        sa.Column("message_id", sa.String(64), primary_key=True),
        sa.Column("review_id", sa.String(64), nullable=False),
        sa.Column("dimension_id", sa.String(64)),
        sa.Column("turn_number", sa.Integer(), nullable=False),
        sa.Column("dim_turn_number", sa.Integer(), server_default="0"),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("message_type", sa.String(16), server_default="MESSAGE"),
        sa.Column("intent", sa.String(32)),
        sa.Column("content", sa.Text()),
        sa.Column("citations", sa.JSON()),
        sa.Column("score_suggestion", sa.JSON()),
        sa.Column("status", sa.String(16), server_default="COMPLETE"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("NOW()")),
    )

    # ==================== 16. award_result 定标结果 ====================
    op.create_table(
        "award_result",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("lot_id", sa.String(64), nullable=False),
        sa.Column("supplier_id", sa.String(64)),
        sa.Column("rank", sa.Integer()),
        sa.Column("score", sa.Numeric(5, 2)),
        sa.Column("bid_amount", sa.Numeric(15, 2)),
        sa.Column("recommendation_reason", sa.Text()),
        sa.Column("status", sa.String(32), server_default="PENDING"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("NOW()")),
    )

    # ==================== 17. outbox_event Outbox 事件表 ====================
    op.create_table(
        "outbox_event",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("aggregate_id", sa.String(64), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), server_default="PENDING"),
        sa.Column("retry_count", sa.Integer(), server_default="0"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("NOW()")),
        sa.Column("processed_at", sa.DateTime()),
    )

    # ==================== 18. notification 站内信通知 ====================
    op.create_table(
        "notification",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("content", sa.Text()),
        sa.Column("is_read", sa.Boolean(), server_default=sa.text("FALSE")),
        sa.Column("related_id", sa.String(64)),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("NOW()")),
    )

    # ==================== 19. system_config 系统配置 ====================
    op.create_table(
        "system_config",
        sa.Column("config_key", sa.String(64), primary_key=True),
        sa.Column("config_value", sa.String(256), nullable=False),
        sa.Column("description", sa.String(512)),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("NOW()")),
        sa.Column("updated_by", sa.String(64)),
    )

    # ==================== 20. expert_profile 专家画像 ====================
    op.create_table(
        "expert_profile",
        sa.Column("expert_id", sa.String(64), primary_key=True),
        sa.Column("total_reviews", sa.Integer(), server_default="0"),
        sa.Column("avg_return_rate", sa.Numeric(4, 3), server_default="0"),
        sa.Column("avg_reasoning_score", sa.Numeric(4, 3), server_default="0.7"),
        sa.Column("review_quality", sa.Numeric(4, 3), server_default="0.7"),
        sa.Column("dimension_stats", sa.JSON()),
        sa.Column("calibration_status", sa.String(16), server_default="UNCALIBRATED"),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("NOW()")),
    )

    # ==================== 21. dimension_calibration 维度校准 ====================
    op.create_table(
        "dimension_calibration",
        sa.Column("dimension_name", sa.String(64), primary_key=True),
        sa.Column("total_projects", sa.Integer(), server_default="0"),
        sa.Column("total_bids", sa.Integer(), server_default="0"),
        sa.Column("score_median", sa.Numeric(5, 2)),
        sa.Column("score_std", sa.Numeric(5, 2)),
        sa.Column("score_min", sa.Numeric(5, 2)),
        sa.Column("score_max", sa.Numeric(5, 2)),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("NOW()")),
    )

    # ==================== 22. audit_log 审计日志 ====================
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(64)),
        sa.Column("action", sa.String(64)),
        sa.Column("target_type", sa.String(32)),
        sa.Column("target_id", sa.String(64)),
        sa.Column("ip_address", sa.String(45)),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("NOW()")),
    )

    # ==================== 二级索引（单表多索引在此补建） ====================
    op.create_index("ix_users_role", "users", ["role"])
    op.create_index("ix_expert_id_number_hash", "expert", ["id_number_hash"])
    op.create_index("ix_bid_lot_id", "bid_document", ["lot_id"])
    op.create_index("ix_bid_supplier_id", "bid_document", ["supplier_id"])
    op.create_index("ix_bid_status", "bid_document", ["status"])
    op.create_index("ix_bid_parsing", "bid_document", ["parsing_step", "updated_at"])
    op.create_index("ix_review_bid_status", "expert_review", ["bid_id", "status"])
    op.create_index("ix_review_expert_id", "expert_review", ["expert_id"])
    op.create_index("ix_review_dimension_id", "expert_review", ["dimension_id"])
    op.create_index("ix_project_status", "project", ["status"])
    op.create_index("ix_lot_project_id", "lot", ["project_id"])
    op.create_index("ix_lot_status", "lot", ["status"])
    op.create_index("ix_dimension_lot_id", "scoring_dimension", ["lot_id"])
    op.create_index("ix_criterion_dimension_id", "scoring_criterion", ["dimension_id"])
    op.create_index("ix_pending_expert_id", "pending_conflict", ["expert_id"])
    op.create_index("ix_pending_credit_code", "pending_conflict", ["credit_code"])
    op.create_index("ix_assignment_expert_id", "lot_expert_assignment", ["expert_id"])
    op.create_index("ix_declaration_expert_id", "expert_conflict_declaration", ["expert_id"])
    op.create_index("ix_declaration_lot_id", "expert_conflict_declaration", ["lot_id"])
    op.create_index("ix_declaration_assignment_id", "expert_conflict_declaration", ["assignment_id"])
    op.create_index("ix_message_review_turn", "conversation_message", ["review_id", "turn_number"])
    op.create_index("ix_message_dim_turn", "conversation_message", ["review_id", "dimension_id", "dim_turn_number"])
    op.create_index("ix_outbox_status_created", "outbox_event", ["status", "created_at"])
    op.create_index("ix_notification_user", "notification", ["user_id", "is_read", "created_at"])
    op.create_index("ix_audit_user_created", "audit_log", ["user_id", "created_at"])
    op.create_index("ix_audit_target", "audit_log", ["target_type", "target_id"])


def downgrade() -> None:
    """删除全部 22 张表（索引随表删除）。"""
    tables = [
        "audit_log",
        "dimension_calibration",
        "expert_profile",
        "system_config",
        "notification",
        "outbox_event",
        "award_result",
        "conversation_message",
        "expert_conflict_declaration",
        "lot_expert_assignment",
        "pending_conflict",
        "lot_expert_criteria",
        "scoring_criterion",
        "scoring_dimension",
        "lot",
        "project",
        "supplier",
        "expert_review",
        "bid_document",
        "expert_specialization",
        "expert",
        "users",
    ]
    for table in tables:
        op.drop_table(table)

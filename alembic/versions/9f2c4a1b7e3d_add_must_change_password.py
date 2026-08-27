"""add must_change_password column

自查 #6 默认密码强改：users 表新增 must_change_password 标记。

server_default=TRUE（fail-closed）：存量/未显式指定（如手工 SQL 建号）的账号
首登强制改密；合成演示账号在导入时显式置 FALSE（scripts/import_synthetic_mysql.py
带该字段），脚本/验收零影响。

Revision ID: 9f2c4a1b7e3d
Revises: ecccf99884f8
Create Date: 2026-08-27

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9f2c4a1b7e3d'
down_revision: Union[str, Sequence[str], None] = 'ecccf99884f8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """users 表加 must_change_password 列（默认 TRUE，首登强改）。"""
    op.add_column(
        "users",
        sa.Column("must_change_password", sa.Boolean(), nullable=False, server_default=sa.text("TRUE")),
    )


def downgrade() -> None:
    """回滚：删列。"""
    op.drop_column("users", "must_change_password")

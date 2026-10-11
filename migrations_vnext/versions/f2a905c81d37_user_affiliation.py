"""Add optional user affiliation, without rewriting existing rows.

Revision ID: f2a905c81d37
Revises: e3c7a91d4b26
"""

from alembic import op
import sqlalchemy as sa

revision = "f2a905c81d37"
down_revision = "e3c7a91d4b26"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """只新增三个可空资料列，旧代码和未填写的老账户仍可正常工作。"""
    op.add_column("users", sa.Column("institution", sa.String(240), nullable=True))
    op.add_column("users", sa.Column("department", sa.String(240), nullable=True))
    op.add_column("users", sa.Column("position", sa.String(160), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_column("position")
        batch.drop_column("department")
        batch.drop_column("institution")

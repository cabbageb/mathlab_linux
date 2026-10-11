"""persistent deletion jobs

Revision ID: b8f34e97ac12
Revises: 7c4e9a1d5b20
Create Date: 2026-08-15
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "b8f34e97ac12"
down_revision: Union[str, Sequence[str], None] = "7c4e9a1d5b20"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "deletion_jobs",
        sa.Column("deletion_job_id", sa.String(length=64), nullable=False),
        sa.Column("owner_user_id", sa.String(length=64), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=False),
        sa.Column("target_id", sa.String(length=64), nullable=False),
        sa.Column("target_name", sa.String(length=240), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("claimed_by_runner", sa.String(length=128), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.user_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("deletion_job_id"),
    )
    for name in ("owner_user_id", "target_type", "target_id", "status", "claimed_by_runner", "created_at"):
        op.create_index(f"ix_deletion_jobs_{name}", "deletion_jobs", [name], unique=False)


def downgrade() -> None:
    for name in reversed(("owner_user_id", "target_type", "target_id", "status", "claimed_by_runner", "created_at")):
        op.drop_index(f"ix_deletion_jobs_{name}", table_name="deletion_jobs")
    op.drop_table("deletion_jobs")

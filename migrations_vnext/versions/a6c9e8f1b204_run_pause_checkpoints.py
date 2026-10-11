"""add durable run pause and agent operation checkpoints

Revision ID: a6c9e8f1b204
Revises: 2f18c9d4a7b1
Create Date: 2026-08-19
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a6c9e8f1b204"
down_revision: Union[str, Sequence[str], None] = "2f18c9d4a7b1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("research_runs") as batch:
        batch.add_column(sa.Column("pause_requested_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("pause_requested_by", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("pause_reason", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("pre_pause_status", sa.String(length=48), nullable=True))
        batch.add_column(sa.Column("pre_pause_phase", sa.String(length=48), nullable=True))
        batch.add_column(sa.Column("resume_requested_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("total_paused_seconds", sa.Float(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("pause_epoch", sa.Integer(), nullable=False, server_default="0"))
        batch.create_index("ix_research_runs_pause_requested_at", ["pause_requested_at"], unique=False)
        batch.create_index("ix_research_runs_paused_at", ["paused_at"], unique=False)
        batch.create_index("ix_research_runs_resume_requested_at", ["resume_requested_at"], unique=False)

    op.create_table(
        "agent_operation_checkpoints",
        sa.Column("operation_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("attempt_id", sa.String(length=64), nullable=False),
        sa.Column("node_id", sa.String(length=64), nullable=True),
        sa.Column("operation_kind", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=96), nullable=False),
        sa.Column("role_key", sa.String(length=128), nullable=False),
        sa.Column("scope_type", sa.String(length=32), nullable=False),
        sa.Column("scope_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="running"),
        sa.Column("provider_thread_id", sa.String(length=256), nullable=True),
        sa.Column("task_path", sa.Text(), nullable=False),
        sa.Column("task_sha256", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("repair_number", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pause_epoch", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["attempt_id"], ["run_attempts.attempt_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["research_runs.run_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("operation_id"),
    )
    op.create_index("ix_agent_operation_checkpoints_run_id", "agent_operation_checkpoints", ["run_id"], unique=False)
    op.create_index("ix_agent_operation_checkpoints_attempt_id", "agent_operation_checkpoints", ["attempt_id"], unique=False)
    op.create_index("ix_agent_operation_checkpoints_node_id", "agent_operation_checkpoints", ["node_id"], unique=False)
    op.create_index("ix_agent_operation_checkpoints_operation_kind", "agent_operation_checkpoints", ["operation_kind"], unique=False)
    op.create_index("ix_agent_operation_checkpoints_status", "agent_operation_checkpoints", ["status"], unique=False)
    op.create_index("ix_agent_operation_checkpoints_provider_thread_id", "agent_operation_checkpoints", ["provider_thread_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_agent_operation_checkpoints_provider_thread_id", table_name="agent_operation_checkpoints")
    op.drop_index("ix_agent_operation_checkpoints_status", table_name="agent_operation_checkpoints")
    op.drop_index("ix_agent_operation_checkpoints_operation_kind", table_name="agent_operation_checkpoints")
    op.drop_index("ix_agent_operation_checkpoints_node_id", table_name="agent_operation_checkpoints")
    op.drop_index("ix_agent_operation_checkpoints_attempt_id", table_name="agent_operation_checkpoints")
    op.drop_index("ix_agent_operation_checkpoints_run_id", table_name="agent_operation_checkpoints")
    op.drop_table("agent_operation_checkpoints")
    with op.batch_alter_table("research_runs") as batch:
        batch.drop_index("ix_research_runs_resume_requested_at")
        batch.drop_index("ix_research_runs_paused_at")
        batch.drop_index("ix_research_runs_pause_requested_at")
        batch.drop_column("pause_epoch")
        batch.drop_column("total_paused_seconds")
        batch.drop_column("resume_requested_at")
        batch.drop_column("pre_pause_phase")
        batch.drop_column("pre_pause_status")
        batch.drop_column("pause_reason")
        batch.drop_column("paused_at")
        batch.drop_column("pause_requested_by")
        batch.drop_column("pause_requested_at")

"""normalize cumulative usage and persist graceful-drain requests

Revision ID: 2f18c9d4a7b1
Revises: 4b6f8d2c1a90
Create Date: 2026-08-19
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "2f18c9d4a7b1"
down_revision: Union[str, Sequence[str], None] = "4b6f8d2c1a90"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("usage_ledger") as batch:
        batch.add_column(sa.Column("provider_thread_id", sa.String(length=256), nullable=True))
        batch.add_column(sa.Column("usage_semantics", sa.String(length=32), nullable=False, server_default="per_turn"))
        batch.add_column(sa.Column("normalization_version", sa.String(length=32), nullable=False, server_default="legacy-v0"))
        batch.add_column(sa.Column("reported_input_tokens", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("reported_output_tokens", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("reported_cached_input_tokens", sa.Integer(), nullable=False, server_default="0"))
        batch.create_index("ix_usage_ledger_provider_thread_id", ["provider_thread_id"], unique=False)
    connection = op.get_bind()
    connection.execute(sa.text("""
        UPDATE usage_ledger
        SET reported_input_tokens = input_tokens,
            reported_output_tokens = output_tokens,
            reported_cached_input_tokens = cached_input_tokens
    """))
    with op.batch_alter_table("research_runs") as batch:
        batch.add_column(sa.Column("graceful_drain_requested_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("graceful_drain_requested_by", sa.String(length=64), nullable=True))
        batch.create_index("ix_research_runs_graceful_drain_requested_at", ["graceful_drain_requested_at"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("research_runs") as batch:
        batch.drop_index("ix_research_runs_graceful_drain_requested_at")
        batch.drop_column("graceful_drain_requested_by")
        batch.drop_column("graceful_drain_requested_at")
    with op.batch_alter_table("usage_ledger") as batch:
        batch.drop_index("ix_usage_ledger_provider_thread_id")
        batch.drop_column("reported_cached_input_tokens")
        batch.drop_column("reported_output_tokens")
        batch.drop_column("reported_input_tokens")
        batch.drop_column("normalization_version")
        batch.drop_column("usage_semantics")
        batch.drop_column("provider_thread_id")

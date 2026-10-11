"""add durable fresh architecture governance state

Revision ID: d4e9a7b6c812
Revises: a6c9e8f1b204
Create Date: 2026-08-19
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d4e9a7b6c812"
down_revision: Union[str, Sequence[str], None] = "a6c9e8f1b204"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("architecture_reviews") as batch:
        batch.add_column(sa.Column("provider_thread_id", sa.String(length=256), nullable=True))
        batch.add_column(sa.Column("trigger_reasons", sa.JSON(), nullable=False, server_default="[]"))
        batch.add_column(sa.Column("input_snapshot_revision", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("input_delta_ids", sa.JSON(), nullable=False, server_default="[]"))
        batch.add_column(sa.Column("input_communication_ids", sa.JSON(), nullable=False, server_default="[]"))
        batch.add_column(sa.Column("evidence_index_path", sa.Text(), nullable=True))
        batch.add_column(sa.Column("evidence_index_sha256", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("evidence_inspected", sa.JSON(), nullable=False, server_default="{}"))
        batch.add_column(sa.Column("snapshot_update_markdown", sa.Text(), nullable=False, server_default=""))
        batch.add_column(sa.Column("ledger_updates_markdown", sa.Text(), nullable=False, server_default=""))

    op.create_table(
        "architecture_snapshots",
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("graph_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("payload", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("snapshot_markdown_path", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("last_review_research_seconds", sa.Float(), nullable=False, server_default="0"),
        sa.Column("last_root_progress_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["research_runs.run_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("run_id"),
    )

    op.create_table(
        "global_state_deltas",
        sa.Column("delta_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("node_id", sa.String(length=64), nullable=True),
        sa.Column("node_version", sa.Integer(), nullable=True),
        sa.Column("graph_version", sa.Integer(), nullable=False),
        sa.Column("change_kind", sa.String(length=64), nullable=False),
        sa.Column("old_summary", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("new_summary", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("evidence_refs", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("gap_key", sa.String(length=300), nullable=True),
        sa.Column("root_relevant", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("consumed_by_review_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["research_runs.run_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("delta_id"),
    )
    for name, columns in (
        ("ix_global_state_deltas_run_id", ["run_id"]),
        ("ix_global_state_deltas_node_id", ["node_id"]),
        ("ix_global_state_deltas_change_kind", ["change_kind"]),
        ("ix_global_state_deltas_gap_key", ["gap_key"]),
        ("ix_global_state_deltas_root_relevant", ["root_relevant"]),
        ("ix_global_state_deltas_status", ["status"]),
    ):
        op.create_index(name, "global_state_deltas", columns, unique=False)

    op.create_table(
        "architecture_communications",
        sa.Column("communication_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("node_id", sa.String(length=64), nullable=False),
        sa.Column("node_version", sa.Integer(), nullable=False),
        sa.Column("graph_version", sa.Integer(), nullable=False),
        sa.Column("attention", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("global_consequence", sa.Text(), nullable=False),
        sa.Column("affected_node_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("related_evidence_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("local_pressure_tests", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("source_markdown_path", sa.Text(), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("consumed_by_review_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["node_id"], ["research_nodes.node_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["research_runs.run_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("communication_id"),
    )
    for name, columns in (
        ("ix_architecture_communications_run_id", ["run_id"]),
        ("ix_architecture_communications_node_id", ["node_id"]),
        ("ix_architecture_communications_attention", ["attention"]),
        ("ix_architecture_communications_status", ["status"]),
    ):
        op.create_index(name, "architecture_communications", columns, unique=False)

    op.create_table(
        "architecture_decision_ledger",
        sa.Column("ledger_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("source_review_id", sa.String(length=64), nullable=False),
        sa.Column("graph_version", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("markdown", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["research_runs.run_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("ledger_id"),
        sa.UniqueConstraint("run_id", "sequence"),
    )
    op.create_index("ix_architecture_decision_ledger_run_id", "architecture_decision_ledger", ["run_id"], unique=False)
    op.create_index("ix_architecture_decision_ledger_source_review_id", "architecture_decision_ledger", ["source_review_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_architecture_decision_ledger_source_review_id", table_name="architecture_decision_ledger")
    op.drop_index("ix_architecture_decision_ledger_run_id", table_name="architecture_decision_ledger")
    op.drop_table("architecture_decision_ledger")
    for name in (
        "ix_architecture_communications_status", "ix_architecture_communications_attention",
        "ix_architecture_communications_node_id", "ix_architecture_communications_run_id",
    ):
        op.drop_index(name, table_name="architecture_communications")
    op.drop_table("architecture_communications")
    for name in (
        "ix_global_state_deltas_status", "ix_global_state_deltas_root_relevant",
        "ix_global_state_deltas_gap_key", "ix_global_state_deltas_change_kind",
        "ix_global_state_deltas_node_id", "ix_global_state_deltas_run_id",
    ):
        op.drop_index(name, table_name="global_state_deltas")
    op.drop_table("global_state_deltas")
    op.drop_table("architecture_snapshots")
    with op.batch_alter_table("architecture_reviews") as batch:
        for column in (
            "ledger_updates_markdown", "snapshot_update_markdown", "evidence_inspected",
            "evidence_index_sha256", "evidence_index_path", "input_communication_ids",
            "input_delta_ids", "input_snapshot_revision", "trigger_reasons", "provider_thread_id",
        ):
            batch.drop_column(column)

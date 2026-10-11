"""harden async mathematical state and publication ownership

Revision ID: f6a1c4d8e2b0
Revises: 8c2e5f1a9b74
Create Date: 2026-08-20
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f6a1c4d8e2b0"
down_revision: Union[str, Sequence[str], None] = "8c2e5f1a9b74"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add durable inbox, verifier certificate, CAS, sequence, OTP and publication state."""
    op.create_table(
        "email_otp_send_reservations",
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("otp_id", sa.String(length=64), nullable=True),
        sa.Column("reserved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("email"),
    )
    op.create_table(
        "scoped_sequences",
        sa.Column("namespace", sa.String(length=64), nullable=False),
        sa.Column("scope_id", sa.String(length=128), nullable=False),
        sa.Column("next_value", sa.BigInteger(), nullable=False, server_default="1"),
        sa.PrimaryKeyConstraint("namespace", "scope_id"),
    )
    op.create_table(
        "publication_states",
        sa.Column("publication_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("attempt_id", sa.String(length=64), nullable=False),
        sa.Column("completion_kind", sa.String(length=48), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="authorized"),
        sa.Column("claimed_by", sa.String(length=128), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("authorized_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("publishing_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["attempt_id"], ["run_attempts.attempt_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["research_runs.run_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("publication_id"),
        sa.UniqueConstraint("run_id", "attempt_id"),
    )
    op.create_index("ix_publication_states_run_id", "publication_states", ["run_id"], unique=False)
    op.create_index("ix_publication_states_attempt_id", "publication_states", ["attempt_id"], unique=False)
    op.create_index("ix_publication_states_status", "publication_states", ["status"], unique=False)

    with op.batch_alter_table("research_nodes") as batch:
        batch.add_column(sa.Column("local_review_count", sa.Integer(), nullable=False, server_default="0"))

    with op.batch_alter_table("worker_reports") as batch:
        batch.add_column(sa.Column(
            "consumption_status", sa.String(length=32), nullable=False,
            server_default="pending_supervisor_review",
        ))
        batch.add_column(sa.Column("consumed_by_supervisor_review_id", sa.String(length=96), nullable=True))
        batch.add_column(sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_index("ix_worker_reports_consumption_status", ["consumption_status"], unique=False)
    # Old rows were produced before a durable inbox existed and were already handled
    # (or irrecoverably ambiguous).  Replaying all of them into resumed Supervisors
    # would be a backwards-incompatible mathematical mutation.
    op.execute(sa.text(
        "UPDATE worker_reports SET consumption_status = 'consumed_legacy'"
    ))

    with op.batch_alter_table("architecture_reviews") as batch:
        batch.add_column(sa.Column("input_node_preconditions", sa.JSON(), nullable=False, server_default="{}"))
        batch.add_column(sa.Column("application_status", sa.String(length=32), nullable=False, server_default="applied"))

    with op.batch_alter_table("architecture_communications") as batch:
        batch.add_column(sa.Column("superseded_by", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("resolved_by_local_review_id", sa.String(length=96), nullable=True))

    with op.batch_alter_table("verified_claims") as batch:
        batch.add_column(sa.Column("statement_sha256", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("proof_sha256", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("candidate_sha256", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("verifier_report_sha256", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("candidate_scope", sa.String(length=32), nullable=False, server_default="auxiliary_fact"))
        batch.add_column(sa.Column("controlling_proposition_id", sa.String(length=128), nullable=True))
        batch.add_column(sa.Column("reuse_boundary", sa.Text(), nullable=False, server_default=""))
        batch.add_column(sa.Column("node_version", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("graph_version", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("verification_policy_version", sa.String(length=64), nullable=False, server_default="claim-v2"))
        batch.add_column(sa.Column("certificate_path", sa.Text(), nullable=True))


def downgrade() -> None:
    """Remove async-integrity state while preserving the older vNext schema."""
    with op.batch_alter_table("verified_claims") as batch:
        for column in (
            "certificate_path", "verification_policy_version", "graph_version",
            "node_version", "reuse_boundary", "controlling_proposition_id",
            "candidate_scope", "verifier_report_sha256", "candidate_sha256",
            "proof_sha256", "statement_sha256",
        ):
            batch.drop_column(column)
    with op.batch_alter_table("architecture_communications") as batch:
        batch.drop_column("resolved_by_local_review_id")
        batch.drop_column("superseded_by")
    with op.batch_alter_table("architecture_reviews") as batch:
        batch.drop_column("application_status")
        batch.drop_column("input_node_preconditions")
    with op.batch_alter_table("worker_reports") as batch:
        batch.drop_index("ix_worker_reports_consumption_status")
        batch.drop_column("consumed_at")
        batch.drop_column("consumed_by_supervisor_review_id")
        batch.drop_column("consumption_status")
    with op.batch_alter_table("research_nodes") as batch:
        batch.drop_column("local_review_count")
    op.drop_index("ix_publication_states_status", table_name="publication_states")
    op.drop_index("ix_publication_states_attempt_id", table_name="publication_states")
    op.drop_index("ix_publication_states_run_id", table_name="publication_states")
    op.drop_table("publication_states")
    op.drop_table("scoped_sequences")
    op.drop_table("email_otp_send_reservations")

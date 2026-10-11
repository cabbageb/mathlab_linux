"""multiuser credit storage runtime gc

Revision ID: 9d8c7b6a5e41
Revises: 5a8d13c72b04
Create Date: 2026-08-18
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "9d8c7b6a5e41"
down_revision: Union[str, Sequence[str], None] = "5a8d13c72b04"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


MONEY = sa.Numeric(18, 6)


def _money_columns() -> tuple[tuple[str, str], ...]:
    return (
        ("users", "credit_balance"),
        ("users", "monthly_cost_limit"),
        ("groups", "default_monthly_cost_limit"),
        ("credit_ledger", "amount"),
        ("research_runs", "expected_cost"),
        ("research_runs", "reserved_credit"),
        ("research_runs", "actual_cost"),
        ("research_runs", "settled_cost"),
        ("usage_ledger", "estimated_cost"),
    )


def upgrade() -> None:
    dialect = op.get_bind().dialect.name

    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("storage_key", sa.String(32), nullable=True))
        batch.add_column(sa.Column("is_developer", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("storage_quota_bytes", sa.BigInteger(), nullable=True, server_default="536870912"))
        batch.create_unique_constraint("uq_users_storage_key", ["storage_key"])
        batch.create_index("ix_users_storage_key", ["storage_key"], unique=True)
        batch.create_index("ix_users_is_developer", ["is_developer"], unique=False)

    op.create_table(
        "user_storage_sequence",
        sa.Column("sequence_name", sa.String(32), primary_key=True),
        sa.Column("next_value", sa.BigInteger(), nullable=False, server_default="1"),
    )

    with op.batch_alter_table("groups") as batch:
        batch.add_column(sa.Column("system_key", sa.String(64), nullable=True))
        batch.create_unique_constraint("uq_groups_system_key", ["system_key"])
        batch.create_index("ix_groups_system_key", ["system_key"], unique=True)

    op.create_table(
        "email_domain_group_rules",
        sa.Column("rule_id", sa.String(64), primary_key=True),
        sa.Column("domain", sa.String(320), nullable=False),
        sa.Column("group_id", sa.String(64), nullable=False),
        sa.Column("welcome_credit", MONEY, nullable=False, server_default="0"),
        sa.Column("grant_key", sa.String(96), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["group_id"], ["groups.group_id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("domain", name="uq_email_domain_group_rules_domain"),
    )
    op.create_index("ix_email_domain_group_rules_domain", "email_domain_group_rules", ["domain"], unique=True)
    op.create_index("ix_email_domain_group_rules_group_id", "email_domain_group_rules", ["group_id"], unique=False)
    op.create_index("ix_email_domain_group_rules_enabled", "email_domain_group_rules", ["enabled"], unique=False)

    with op.batch_alter_table("credit_ledger", recreate="always") as batch:
        batch.alter_column("user_id", existing_type=sa.String(64), nullable=True)
        batch.add_column(sa.Column("subject_email", sa.String(320), nullable=True))
        batch.add_column(sa.Column("subject_storage_key", sa.String(32), nullable=True))
        batch.add_column(sa.Column("source_key", sa.String(160), nullable=True))
        batch.create_unique_constraint("uq_credit_ledger_source_key", ["source_key"])
        batch.create_index("ix_credit_ledger_source_key", ["source_key"], unique=True)

    generation_columns = (
        sa.Column("expected_credit", MONEY, nullable=False, server_default="0"),
        sa.Column("reserved_credit", MONEY, nullable=False, server_default="0"),
        sa.Column("actual_credit", MONEY, nullable=False, server_default="0"),
        sa.Column("settled_credit", MONEY, nullable=False, server_default="0"),
        sa.Column("runtime_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("runtime_cleaned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("keep_runtime", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("runtime_cleanup_claimed_by", sa.String(128), nullable=True),
        sa.Column("runtime_cleanup_claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("runtime_cleanup_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("runtime_cleanup_error", sa.Text(), nullable=True),
    )
    with op.batch_alter_table("contract_generation_attempts") as batch:
        batch.alter_column("request_snapshot_path", existing_type=sa.Text(), nullable=True)
        for column in generation_columns:
            batch.add_column(column)
        batch.create_index("ix_contract_generation_attempts_runtime_expires_at", ["runtime_expires_at"], unique=False)
        batch.create_index("ix_contract_generation_attempts_runtime_cleanup_claimed_by", ["runtime_cleanup_claimed_by"], unique=False)

    op.create_table(
        "contract_chat_calls",
        sa.Column("chat_call_id", sa.String(64), primary_key=True),
        sa.Column("conversation_id", sa.String(64), nullable=False),
        sa.Column("contract_version_id", sa.String(64), nullable=False),
        sa.Column("owner_user_id", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("profile", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="reserved"),
        sa.Column("expected_credit", MONEY, nullable=False, server_default="0"),
        sa.Column("reserved_credit", MONEY, nullable=False, server_default="0"),
        sa.Column("actual_credit", MONEY, nullable=False, server_default="0"),
        sa.Column("settled_credit", MONEY, nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["conversation_id"], ["contract_conversations.conversation_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["contract_version_id"], ["contract_versions.contract_version_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.user_id"], ondelete="CASCADE"),
    )
    for name in ("conversation_id", "contract_version_id", "owner_user_id", "status", "created_at"):
        op.create_index(f"ix_contract_chat_calls_{name}", "contract_chat_calls", [name], unique=False)

    run_columns = (
        sa.Column("terminal_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("runtime_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("runtime_cleaned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("keep_runtime", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("final_snapshot_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("runtime_cleanup_claimed_by", sa.String(128), nullable=True),
        sa.Column("runtime_cleanup_claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("runtime_cleanup_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("runtime_cleanup_error", sa.Text(), nullable=True),
    )
    with op.batch_alter_table("research_runs") as batch:
        for column in run_columns:
            batch.add_column(column)
        batch.create_index("ix_research_runs_terminal_at", ["terminal_at"], unique=False)
        batch.create_index("ix_research_runs_runtime_expires_at", ["runtime_expires_at"], unique=False)
        batch.create_index("ix_research_runs_runtime_cleanup_claimed_by", ["runtime_cleanup_claimed_by"], unique=False)

    with op.batch_alter_table("verified_claims") as batch:
        batch.add_column(sa.Column("title", sa.String(300), nullable=False, server_default="Verified mathematical result"))
        batch.add_column(sa.Column("public_summary", sa.Text(), nullable=True))
        batch.alter_column("candidate_path", existing_type=sa.Text(), nullable=True)
        batch.alter_column("verifier_report_path", existing_type=sa.Text(), nullable=True)

    for table, column in _money_columns():
        kwargs = {"existing_type": sa.Float(), "type_": MONEY}
        if dialect == "postgresql":
            kwargs["postgresql_using"] = f"{column}::numeric(18,6)"
        with op.batch_alter_table(table) as batch:
            batch.alter_column(column, **kwargs)

    op.execute(sa.text(
        "UPDATE research_runs SET terminal_at = completed_at "
        "WHERE status IN ('completed','failed','cancelled','timed_out') AND completed_at IS NOT NULL"
    ))


def downgrade() -> None:
    for table, column in reversed(_money_columns()):
        with op.batch_alter_table(table) as batch:
            batch.alter_column(column, existing_type=MONEY, type_=sa.Float())

    with op.batch_alter_table("verified_claims") as batch:
        batch.alter_column("verifier_report_path", existing_type=sa.Text(), nullable=False)
        batch.alter_column("candidate_path", existing_type=sa.Text(), nullable=False)
        batch.drop_column("public_summary")
        batch.drop_column("title")

    with op.batch_alter_table("research_runs") as batch:
        batch.drop_index("ix_research_runs_runtime_cleanup_claimed_by")
        batch.drop_index("ix_research_runs_runtime_expires_at")
        batch.drop_index("ix_research_runs_terminal_at")
        for name in (
            "runtime_cleanup_error", "runtime_cleanup_attempts", "runtime_cleanup_claimed_at",
            "runtime_cleanup_claimed_by", "final_snapshot_completed_at", "keep_runtime",
            "runtime_cleaned_at", "runtime_expires_at", "terminal_at",
        ):
            batch.drop_column(name)

    op.drop_table("contract_chat_calls")

    with op.batch_alter_table("contract_generation_attempts") as batch:
        batch.drop_index("ix_contract_generation_attempts_runtime_cleanup_claimed_by")
        batch.drop_index("ix_contract_generation_attempts_runtime_expires_at")
        for name in (
            "runtime_cleanup_error", "runtime_cleanup_attempts", "runtime_cleanup_claimed_at",
            "runtime_cleanup_claimed_by", "keep_runtime", "runtime_cleaned_at",
            "runtime_expires_at", "settled_credit", "actual_credit", "reserved_credit",
            "expected_credit",
        ):
            batch.drop_column(name)
        batch.alter_column("request_snapshot_path", existing_type=sa.Text(), nullable=False)

    with op.batch_alter_table("credit_ledger", recreate="always") as batch:
        batch.drop_index("ix_credit_ledger_source_key")
        batch.drop_constraint("uq_credit_ledger_source_key", type_="unique")
        batch.drop_column("source_key")
        batch.drop_column("subject_storage_key")
        batch.drop_column("subject_email")
        batch.alter_column("user_id", existing_type=sa.String(64), nullable=False)

    op.drop_table("email_domain_group_rules")
    with op.batch_alter_table("groups") as batch:
        batch.drop_index("ix_groups_system_key")
        batch.drop_constraint("uq_groups_system_key", type_="unique")
        batch.drop_column("system_key")
    op.drop_table("user_storage_sequence")
    with op.batch_alter_table("users") as batch:
        batch.drop_index("ix_users_is_developer")
        batch.drop_index("ix_users_storage_key")
        batch.drop_constraint("uq_users_storage_key", type_="unique")
        batch.drop_column("storage_quota_bytes")
        batch.drop_column("is_developer")
        batch.drop_column("storage_key")

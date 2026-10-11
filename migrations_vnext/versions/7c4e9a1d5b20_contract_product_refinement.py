"""contract product refinement

Revision ID: 7c4e9a1d5b20
Revises: 3eea365e674f
Create Date: 2026-08-15
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "7c4e9a1d5b20"
down_revision: Union[str, Sequence[str], None] = "3eea365e674f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("contract_chat_panel_width", sa.Float(), nullable=False, server_default="0.34"))

    with op.batch_alter_table("email_otps") as batch:
        # Existing OTP rows predate delivery tracking; treat them as delivered.
        batch.add_column(sa.Column("status", sa.String(length=32), nullable=False, server_default="delivered"))
        batch.add_column(sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("delivery_error_class", sa.String(length=160), nullable=True))
        batch.create_index("ix_email_otps_status", ["status"], unique=False)

    with op.batch_alter_table("contract_conversations") as batch:
        batch.add_column(sa.Column("research_problem", sa.Text(), nullable=False, server_default=""))
        batch.add_column(sa.Column("background_and_prior_progress", sa.Text(), nullable=False, server_default=""))
        batch.add_column(sa.Column("method_preferences_and_restrictions", sa.Text(), nullable=False, server_default=""))
        batch.add_column(sa.Column("valuable_partial_outcomes", sa.Text(), nullable=False, server_default=""))
        batch.add_column(sa.Column("possible_ideas_and_rough_approaches", sa.Text(), nullable=False, server_default=""))
        batch.add_column(sa.Column("contractor_profile", sa.String(length=64), nullable=False, server_default="high"))
        batch.add_column(sa.Column("request_draft_revision", sa.Integer(), nullable=False, server_default="1"))

    with op.batch_alter_table("contract_versions") as batch:
        batch.add_column(sa.Column("execution_policy", sa.JSON(), nullable=False, server_default=sa.text("'{}'")))

    with op.batch_alter_table("working_drafts") as batch:
        batch.add_column(sa.Column("execution_policy", sa.JSON(), nullable=False, server_default=sa.text("'{}'")))
        batch.add_column(sa.Column("draft_revision", sa.Integer(), nullable=False, server_default="1"))

    with op.batch_alter_table("research_runs") as batch:
        batch.add_column(sa.Column("execution_policy", sa.JSON(), nullable=False, server_default=sa.text("'{}'")))

    op.create_table(
        "contract_chat_messages",
        sa.Column("chat_message_id", sa.String(length=64), nullable=False),
        sa.Column("conversation_id", sa.String(length=64), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("draft_revision_before", sa.Integer(), nullable=True),
        sa.Column("draft_revision_after", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["contract_conversations.conversation_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("chat_message_id"),
        sa.UniqueConstraint("conversation_id", "sequence"),
    )
    op.create_index("ix_contract_chat_messages_conversation_id", "contract_chat_messages", ["conversation_id"], unique=False)

    op.create_table(
        "contract_generation_attempts",
        sa.Column("generation_attempt_id", sa.String(length=64), nullable=False),
        sa.Column("conversation_id", sa.String(length=64), nullable=False),
        sa.Column("owner_user_id", sa.String(length=64), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("contractor_profile", sa.String(length=64), nullable=False),
        sa.Column("request_snapshot_path", sa.Text(), nullable=False),
        sa.Column("source_manifest", sa.JSON(), nullable=False),
        sa.Column("base_contract_version_id", sa.String(length=64), nullable=True),
        sa.Column("base_draft_revision", sa.Integer(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("claimed_by_runner", sa.String(length=128), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resulting_contract_version_id", sa.String(length=64), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["conversation_id"], ["contract_conversations.conversation_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.user_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("generation_attempt_id"),
        sa.UniqueConstraint("conversation_id", "attempt_number"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index("ix_contract_generation_attempts_conversation_id", "contract_generation_attempts", ["conversation_id"], unique=False)
    op.create_index("ix_contract_generation_attempts_owner_user_id", "contract_generation_attempts", ["owner_user_id"], unique=False)
    op.create_index("ix_contract_generation_attempts_status", "contract_generation_attempts", ["status"], unique=False)
    op.create_index("ix_contract_generation_attempts_claimed_by_runner", "contract_generation_attempts", ["claimed_by_runner"], unique=False)
    op.create_index("ix_contract_generation_attempts_created_at", "contract_generation_attempts", ["created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_contract_generation_attempts_created_at", table_name="contract_generation_attempts")
    op.drop_index("ix_contract_generation_attempts_claimed_by_runner", table_name="contract_generation_attempts")
    op.drop_index("ix_contract_generation_attempts_status", table_name="contract_generation_attempts")
    op.drop_index("ix_contract_generation_attempts_owner_user_id", table_name="contract_generation_attempts")
    op.drop_index("ix_contract_generation_attempts_conversation_id", table_name="contract_generation_attempts")
    op.drop_table("contract_generation_attempts")
    op.drop_index("ix_contract_chat_messages_conversation_id", table_name="contract_chat_messages")
    op.drop_table("contract_chat_messages")

    with op.batch_alter_table("research_runs") as batch:
        batch.drop_column("execution_policy")
    with op.batch_alter_table("working_drafts") as batch:
        batch.drop_column("draft_revision")
        batch.drop_column("execution_policy")
    with op.batch_alter_table("contract_versions") as batch:
        batch.drop_column("execution_policy")
    with op.batch_alter_table("contract_conversations") as batch:
        batch.drop_column("request_draft_revision")
        batch.drop_column("contractor_profile")
        batch.drop_column("possible_ideas_and_rough_approaches")
        batch.drop_column("valuable_partial_outcomes")
        batch.drop_column("method_preferences_and_restrictions")
        batch.drop_column("background_and_prior_progress")
        batch.drop_column("research_problem")
    with op.batch_alter_table("email_otps") as batch:
        batch.drop_index("ix_email_otps_status")
        batch.drop_column("delivery_error_class")
        batch.drop_column("delivered_at")
        batch.drop_column("status")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("contract_chat_panel_width")

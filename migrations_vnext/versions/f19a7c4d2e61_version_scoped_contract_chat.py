"""scope Contract Chat by immutable Contract version

Revision ID: f19a7c4d2e61
Revises: e84c2a1f7d90
Create Date: 2026-08-16
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f19a7c4d2e61"
down_revision: Union[str, Sequence[str], None] = "e84c2a1f7d90"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("contract_chat_messages") as batch:
        batch.add_column(sa.Column("contract_version_id", sa.String(length=64), nullable=True))
        batch.create_foreign_key(
            "fk_contract_chat_messages_contract_version_id",
            "contract_versions",
            ["contract_version_id"],
            ["contract_version_id"],
            ondelete="CASCADE",
        )
        batch.create_index(
            "ix_contract_chat_messages_contract_version_id",
            ["contract_version_id"],
            unique=False,
        )

    connection = op.get_bind()
    connection.execute(
        sa.text(
            """
            UPDATE contract_chat_messages
            SET contract_version_id = (
                SELECT contract_versions.contract_version_id
                FROM contract_versions
                WHERE contract_versions.conversation_id = contract_chat_messages.conversation_id
                ORDER BY contract_versions.version DESC
                LIMIT 1
            )
            WHERE contract_version_id IS NULL
            """
        )
    )
    with op.batch_alter_table("contract_chat_messages") as batch:
        batch.alter_column(
            "contract_version_id",
            existing_type=sa.String(length=64),
            nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("contract_chat_messages") as batch:
        batch.drop_index("ix_contract_chat_messages_contract_version_id")
        batch.drop_constraint(
            "fk_contract_chat_messages_contract_version_id", type_="foreignkey"
        )
        batch.drop_column("contract_version_id")

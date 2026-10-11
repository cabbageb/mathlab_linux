"""store execution policy on the contract conversation

Revision ID: d7f4129a8b63
Revises: c91d2a0f4e37
Create Date: 2026-08-15
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d7f4129a8b63"
down_revision: Union[str, Sequence[str], None] = "c91d2a0f4e37"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("contract_conversations") as batch:
        batch.add_column(
            sa.Column(
                "execution_policy",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'"),
            )
        )

    connection = op.get_bind()
    conversations = sa.table(
        "contract_conversations",
        sa.column("conversation_id", sa.String()),
        sa.column("latest_contract_version", sa.Integer()),
        sa.column("execution_policy", sa.JSON()),
    )
    versions = sa.table(
        "contract_versions",
        sa.column("conversation_id", sa.String()),
        sa.column("version", sa.Integer()),
        sa.column("title", sa.String()),
        sa.column("sections", sa.JSON()),
        sa.column("source_manifest", sa.JSON()),
        sa.column("execution_policy", sa.JSON()),
    )
    drafts = sa.table(
        "working_drafts",
        sa.column("conversation_id", sa.String()),
        sa.column("base_version", sa.Integer()),
        sa.column("title", sa.String()),
        sa.column("sections", sa.JSON()),
        sa.column("source_manifest", sa.JSON()),
        sa.column("execution_policy", sa.JSON()),
    )

    for conversation in connection.execute(sa.select(conversations)).mappings():
        conversation_id = conversation["conversation_id"]
        draft = connection.execute(
            sa.select(drafts).where(drafts.c.conversation_id == conversation_id)
        ).mappings().first()
        version_number = (
            draft["base_version"]
            if draft is not None
            else conversation["latest_contract_version"]
        )
        version = None
        if version_number:
            version = connection.execute(
                sa.select(versions).where(
                    versions.c.conversation_id == conversation_id,
                    versions.c.version == version_number,
                )
            ).mappings().first()

        policy = dict((draft or {}).get("execution_policy") or {})
        if not policy and version is not None:
            policy = dict(version.get("execution_policy") or {})
        connection.execute(
            sa.update(conversations)
            .where(conversations.c.conversation_id == conversation_id)
            .values(execution_policy=policy)
        )

        if (
            draft is not None
            and version is not None
            and draft["title"] == version["title"]
            and draft["sections"] == version["sections"]
            and draft["source_manifest"] == version["source_manifest"]
        ):
            connection.execute(
                sa.delete(drafts).where(drafts.c.conversation_id == conversation_id)
            )


def downgrade() -> None:
    with op.batch_alter_table("contract_conversations") as batch:
        batch.drop_column("execution_policy")

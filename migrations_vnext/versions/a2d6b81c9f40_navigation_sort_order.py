"""persist Project and Contract Conversation navigation order

Revision ID: a2d6b81c9f40
Revises: f19a7c4d2e61
Create Date: 2026-08-16
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a2d6b81c9f40"
down_revision: Union[str, Sequence[str], None] = "f19a7c4d2e61"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("projects") as batch:
        batch.add_column(
            sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0")
        )
    with op.batch_alter_table("contract_conversations") as batch:
        batch.add_column(
            sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0")
        )

    connection = op.get_bind()
    projects = sa.table(
        "projects",
        sa.column("project_id", sa.String()),
        sa.column("owner_user_id", sa.String()),
        sa.column("sort_order", sa.Integer()),
        sa.column("updated_at", sa.DateTime()),
    )
    conversations = sa.table(
        "contract_conversations",
        sa.column("conversation_id", sa.String()),
        sa.column("owner_user_id", sa.String()),
        sa.column("project_id", sa.String()),
        sa.column("sort_order", sa.Integer()),
        sa.column("updated_at", sa.DateTime()),
    )

    owner_ids = [
        row[0]
        for row in connection.execute(
            sa.select(projects.c.owner_user_id).distinct()
        )
    ]
    conversation_owner_ids = [
        row[0]
        for row in connection.execute(
            sa.select(conversations.c.owner_user_id).distinct()
        )
    ]
    for owner_user_id in dict.fromkeys(owner_ids + conversation_owner_ids):
        project_rows = connection.execute(
            sa.select(projects.c.project_id)
            .where(projects.c.owner_user_id == owner_user_id)
            .order_by(projects.c.updated_at.desc(), projects.c.project_id)
        ).all()
        for index, row in enumerate(project_rows):
            connection.execute(
                sa.update(projects)
                .where(projects.c.project_id == row.project_id)
                .values(sort_order=index)
            )

        groups = [None] + [row.project_id for row in project_rows]
        for project_id in groups:
            condition = conversations.c.project_id.is_(None) if project_id is None else (
                conversations.c.project_id == project_id
            )
            rows = connection.execute(
                sa.select(conversations.c.conversation_id)
                .where(
                    conversations.c.owner_user_id == owner_user_id,
                    condition,
                )
                .order_by(
                    conversations.c.updated_at.desc(),
                    conversations.c.conversation_id,
                )
            ).all()
            for index, row in enumerate(rows):
                connection.execute(
                    sa.update(conversations)
                    .where(conversations.c.conversation_id == row.conversation_id)
                    .values(sort_order=index)
                )


def downgrade() -> None:
    with op.batch_alter_table("contract_conversations") as batch:
        batch.drop_column("sort_order")
    with op.batch_alter_table("projects") as batch:
        batch.drop_column("sort_order")

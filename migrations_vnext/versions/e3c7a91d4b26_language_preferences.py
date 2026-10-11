"""persist website, Contract and Research languages

Revision ID: e3c7a91d4b26
Revises: b3e7d4a9c621
Create Date: 2026-09-03
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e3c7a91d4b26"
down_revision: Union[str, Sequence[str], None] = "b3e7d4a9c621"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("website_language", sa.String(length=16), nullable=False, server_default="zh"))
        batch.add_column(sa.Column("default_contract_language", sa.String(length=16), nullable=False, server_default="zh"))
        batch.add_column(sa.Column("default_research_language", sa.String(length=16), nullable=False, server_default="zh"))
    with op.batch_alter_table("contract_conversations") as batch:
        batch.add_column(sa.Column("contract_language", sa.String(length=16), nullable=False, server_default="zh"))
    with op.batch_alter_table("contract_versions") as batch:
        batch.add_column(sa.Column("contract_language", sa.String(length=16), nullable=False, server_default="zh"))
    with op.batch_alter_table("working_drafts") as batch:
        batch.add_column(sa.Column("contract_language", sa.String(length=16), nullable=False, server_default="zh"))
    with op.batch_alter_table("research_runs") as batch:
        batch.add_column(sa.Column("research_language", sa.String(length=16), nullable=False, server_default="zh"))

    op.execute(sa.text("UPDATE users SET website_language='zh', default_contract_language='zh', default_research_language='zh'"))
    op.execute(sa.text("UPDATE contract_conversations SET contract_language='zh'"))
    op.execute(sa.text("UPDATE contract_versions SET contract_language='zh'"))
    op.execute(sa.text("UPDATE working_drafts SET contract_language='zh'"))
    op.execute(sa.text("UPDATE research_runs SET research_language='zh'"))


def downgrade() -> None:
    with op.batch_alter_table("research_runs") as batch:
        batch.drop_column("research_language")
    with op.batch_alter_table("working_drafts") as batch:
        batch.drop_column("contract_language")
    with op.batch_alter_table("contract_versions") as batch:
        batch.drop_column("contract_language")
    with op.batch_alter_table("contract_conversations") as batch:
        batch.drop_column("contract_language")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("default_research_language")
        batch.drop_column("default_contract_language")
        batch.drop_column("website_language")

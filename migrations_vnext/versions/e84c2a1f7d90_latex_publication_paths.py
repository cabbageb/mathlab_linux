"""latex publication paths

Revision ID: e84c2a1f7d90
Revises: d7f4129a8b63
Create Date: 2026-08-16
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e84c2a1f7d90"
down_revision: Union[str, Sequence[str], None] = "d7f4129a8b63"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("research_runs", sa.Column("final_paper_pdf_path", sa.Text(), nullable=True))
    op.add_column("research_runs", sa.Column("final_paper_tex_path", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("research_runs", "final_paper_tex_path")
    op.drop_column("research_runs", "final_paper_pdf_path")

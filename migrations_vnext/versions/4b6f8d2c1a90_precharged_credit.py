"""record real upfront Credit charges separately from legacy reservations

Revision ID: 4b6f8d2c1a90
Revises: 9d8c7b6a5e41
Create Date: 2026-08-18
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "4b6f8d2c1a90"
down_revision: Union[str, Sequence[str], None] = "9d8c7b6a5e41"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


MONEY = sa.Numeric(18, 6)


def upgrade() -> None:
    for table in ("contract_generation_attempts", "contract_chat_calls", "research_runs"):
        with op.batch_alter_table(table) as batch:
            batch.add_column(
                sa.Column("precharged_credit", MONEY, nullable=False, server_default="0")
            )


def downgrade() -> None:
    for table in ("research_runs", "contract_chat_calls", "contract_generation_attempts"):
        with op.batch_alter_table(table) as batch:
            batch.drop_column("precharged_credit")

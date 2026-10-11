"""bind future verified claims to one immutable candidate artifact

Revision ID: 8c2e5f1a9b74
Revises: d4e9a7b6c812
Create Date: 2026-08-20
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "8c2e5f1a9b74"
down_revision: Union[str, Sequence[str], None] = "d4e9a7b6c812"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """为新 Claim 增加 Candidate 身份约束，不回填或合并历史重复记录。"""
    with op.batch_alter_table("verified_claims") as batch:
        batch.add_column(
            sa.Column("candidate_artifact_id", sa.String(length=96), nullable=True)
        )
        batch.create_unique_constraint(
            "uq_verified_claims_run_candidate_artifact",
            ["run_id", "candidate_artifact_id"],
        )


def downgrade() -> None:
    """移除 Candidate 身份约束并恢复旧表结构。"""
    with op.batch_alter_table("verified_claims") as batch:
        batch.drop_constraint(
            "uq_verified_claims_run_candidate_artifact", type_="unique"
        )
        batch.drop_column("candidate_artifact_id")

"""persist resumable Contract generation stages

Revision ID: 5a8d13c72b04
Revises: a2d6b81c9f40
Create Date: 2026-08-16
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import uuid


revision: str = "5a8d13c72b04"
down_revision: Union[str, Sequence[str], None] = "a2d6b81c9f40"
branch_labels = None
depends_on = None


STAGES = (
    "contractor_stage1",
    "contractor_stage2",
    "auditor_fidelity",
    "auditor_completeness",
    "auditor_map",
    "integrator",
)


def upgrade() -> None:
    op.create_table(
        "contract_generation_stages",
        sa.Column("generation_stage_id", sa.String(length=64), nullable=False),
        sa.Column("generation_attempt_id", sa.String(length=64), nullable=False),
        sa.Column("stage_key", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("provider_thread_id", sa.String(length=256), nullable=True),
        sa.Column("artifact_path", sa.Text(), nullable=True),
        sa.Column("artifact_sha256", sa.String(length=64), nullable=True),
        sa.Column("repair_number", sa.Integer(), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("reasoning_effort", sa.String(length=64), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["generation_attempt_id"],
            ["contract_generation_attempts.generation_attempt_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("generation_stage_id"),
        sa.UniqueConstraint("generation_attempt_id", "stage_key"),
    )
    op.create_index(
        "ix_contract_generation_stages_generation_attempt_id",
        "contract_generation_stages",
        ["generation_attempt_id"],
    )
    op.create_index(
        "ix_contract_generation_stages_stage_key",
        "contract_generation_stages",
        ["stage_key"],
    )
    op.create_index(
        "ix_contract_generation_stages_status",
        "contract_generation_stages",
        ["status"],
    )

    connection = op.get_bind()
    attempts = connection.execute(
        sa.text("SELECT generation_attempt_id, status FROM contract_generation_attempts")
    ).mappings()
    rows = [
        {
            "generation_stage_id": f"genstage_{uuid.uuid4().hex}",
            "generation_attempt_id": attempt["generation_attempt_id"],
            "stage_key": stage,
            "status": "completed" if attempt["status"] == "completed" else "waiting",
        }
        for attempt in attempts
        for stage in STAGES
    ]
    if rows:
        connection.execute(
            sa.text(
                """
                INSERT INTO contract_generation_stages (
                    generation_stage_id, generation_attempt_id, stage_key, status,
                    repair_number, updated_at
                ) VALUES (
                    :generation_stage_id, :generation_attempt_id, :stage_key, :status,
                    0, CURRENT_TIMESTAMP
                )
                """
            ),
            rows,
        )


def downgrade() -> None:
    op.drop_index("ix_contract_generation_stages_status", table_name="contract_generation_stages")
    op.drop_index("ix_contract_generation_stages_stage_key", table_name="contract_generation_stages")
    op.drop_index(
        "ix_contract_generation_stages_generation_attempt_id",
        table_name="contract_generation_stages",
    )
    op.drop_table("contract_generation_stages")

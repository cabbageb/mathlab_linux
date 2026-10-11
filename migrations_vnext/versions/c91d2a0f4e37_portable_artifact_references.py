"""portable artifact references

Revision ID: c91d2a0f4e37
Revises: b8f34e97ac12
Create Date: 2026-08-15
"""
from typing import Sequence, Union

from alembic import op

from math_lab_core.vnext.artifact_migration import migrate_database_references

revision: str = "c91d2a0f4e37"
down_revision: Union[str, Sequence[str], None] = "b8f34e97ac12"
branch_labels = None
depends_on = None


def upgrade() -> None:
    migrate_database_references(op.get_bind(), apply=True)


def downgrade() -> None:
    # Relative references remain valid for older code through ArtifactStore.resolve.
    # Reconstructing an unknown former host root would make a downgrade non-portable.
    pass

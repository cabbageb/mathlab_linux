"""align credit ledger ownership foreign key

Revision ID: b3e7d4a9c621
Revises: f6a1c4d8e2b0
Create Date: 2026-09-02
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b3e7d4a9c621"
down_revision: Union[str, Sequence[str], None] = "f6a1c4d8e2b0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


NAMING_CONVENTION = {
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
}


def _replace_user_foreign_key(ondelete: str) -> None:
    """将 credit ledger 的用户外键替换为 ORM 当前声明的删除语义。"""
    bind = op.get_bind()
    foreign_keys = sa.inspect(bind).get_foreign_keys("credit_ledger")
    current = next(
        item for item in foreign_keys
        if item.get("constrained_columns") == ["user_id"]
    )
    current_name = current.get("name") or "fk_credit_ledger_user_id_users"
    recreate = "always" if bind.dialect.name == "sqlite" else "auto"
    with op.batch_alter_table(
        "credit_ledger",
        recreate=recreate,
        naming_convention=NAMING_CONVENTION,
    ) as batch:
        batch.drop_constraint(current_name, type_="foreignkey")
        batch.create_foreign_key(
            "credit_ledger_user_id_fkey",
            "users",
            ["user_id"],
            ["user_id"],
            ondelete=ondelete,
        )


def upgrade() -> None:
    _replace_user_foreign_key("SET NULL")


def downgrade() -> None:
    _replace_user_foreign_key("RESTRICT")

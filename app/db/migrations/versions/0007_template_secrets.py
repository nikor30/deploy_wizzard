"""template secrets table

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-18

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "template_secrets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("secret_encrypted", sa.String(length=2048), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_template_secrets_name", "template_secrets", ["name"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_template_secrets_name", table_name="template_secrets")
    op.drop_table("template_secrets")

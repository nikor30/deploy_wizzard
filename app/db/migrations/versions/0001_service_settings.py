"""service_settings table

Revision ID: 0001
Revises:
Create Date: 2026-07-16

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "service_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("service", sa.String(length=16), nullable=False),
        sa.Column("base_url", sa.String(length=512), nullable=True),
        sa.Column("username", sa.String(length=256), nullable=True),
        sa.Column("secret_encrypted", sa.String(length=2048), nullable=True),
        sa.Column("tls_verify", sa.Boolean(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_service_settings_service", "service_settings", ["service"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_service_settings_service", table_name="service_settings")
    op.drop_table("service_settings")

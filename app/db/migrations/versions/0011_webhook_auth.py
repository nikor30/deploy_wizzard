"""webhook auth header + encrypted token on service_settings

Revision ID: 0011
Revises: 0010
Create Date: 2026-07-28

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("service_settings", sa.Column("auth_header", sa.String(length=64), nullable=True))
    op.add_column(
        "service_settings", sa.Column("auth_token_encrypted", sa.String(length=2048), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("service_settings", "auth_token_encrypted")
    op.drop_column("service_settings", "auth_header")

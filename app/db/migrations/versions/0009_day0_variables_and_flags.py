"""day0_variables column + app_settings flags table

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-20

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("job_devices", sa.Column("day0_variables", sa.JSON(), nullable=True))
    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(length=64), primary_key=True),
        sa.Column("value", sa.String(length=256), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("app_settings")
    op.drop_column("job_devices", "day0_variables")

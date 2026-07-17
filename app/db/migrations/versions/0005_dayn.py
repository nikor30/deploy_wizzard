"""day-n mapping table + job/device columns

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-16

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("dayn_template_id", sa.String(length=64), nullable=True))
    op.add_column("job_devices", sa.Column("dayn_variables", sa.JSON(), nullable=True))
    op.create_table(
        "dayn_mappings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("variable", sa.String(length=128), nullable=False),
        sa.Column("source_path", sa.String(length=256), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_dayn_mappings_variable", "dayn_mappings", ["variable"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_dayn_mappings_variable", table_name="dayn_mappings")
    op.drop_table("dayn_mappings")
    op.drop_column("job_devices", "dayn_variables")
    op.drop_column("jobs", "dayn_template_id")

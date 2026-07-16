"""site_mappings table

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-16

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "site_mappings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("netbox_site_id", sa.Integer(), nullable=False),
        sa.Column("netbox_site_name", sa.String(length=256), nullable=False),
        sa.Column("ccc_site_id", sa.String(length=64), nullable=False),
        sa.Column("ccc_site_name", sa.String(length=512), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_site_mappings_netbox_site_id", "site_mappings", ["netbox_site_id"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_site_mappings_netbox_site_id", table_name="site_mappings")
    op.drop_table("site_mappings")

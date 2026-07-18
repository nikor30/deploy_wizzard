"""location-aware site mappings

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-19

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("site_mappings") as batch:
        batch.add_column(sa.Column("netbox_location_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("netbox_location_name", sa.String(length=256), nullable=True))
        # netbox_site_id is no longer unique: one row per site or per location
        batch.drop_index("ix_site_mappings_netbox_site_id")
    op.create_index("ix_site_mappings_netbox_site_id", "site_mappings", ["netbox_site_id"])
    op.create_index("ix_site_mappings_netbox_location_id", "site_mappings", ["netbox_location_id"])


def downgrade() -> None:
    op.drop_index("ix_site_mappings_netbox_location_id", table_name="site_mappings")
    with op.batch_alter_table("site_mappings") as batch:
        batch.drop_column("netbox_location_name")
        batch.drop_column("netbox_location_id")

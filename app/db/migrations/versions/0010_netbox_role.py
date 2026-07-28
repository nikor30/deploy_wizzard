"""netbox_role on job_devices (mirrored into CCC inventory as role + tag)

Revision ID: 0010
Revises: 0009
Create Date: 2026-07-28

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("job_devices", sa.Column("netbox_role", sa.String(length=128), nullable=True))


def downgrade() -> None:
    op.drop_column("job_devices", "netbox_role")

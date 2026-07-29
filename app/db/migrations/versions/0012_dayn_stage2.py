"""second Day-N stage (ports/uplinks) so a risky push is separate from VLANs

Revision ID: 0012
Revises: 0011
Create Date: 2026-07-28

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("dayn2_template_id", sa.String(length=64), nullable=True))
    op.add_column("job_devices", sa.Column("dayn2_variables", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("job_devices", "dayn2_variables")
    op.drop_column("jobs", "dayn2_template_id")

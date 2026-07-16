"""day0 columns + webhook deliveries

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-16

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("day0_config_id", sa.String(length=64), nullable=True))
    op.add_column("jobs", sa.Column("day0_image_id", sa.String(length=64), nullable=True))
    op.add_column("job_devices", sa.Column("error", sa.String(length=2048), nullable=True))
    op.add_column(
        "job_devices", sa.Column("day0_started_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "job_devices", sa.Column("day0_finished_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_table(
        "webhook_deliveries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("device_serial", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(length=1024), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_webhook_deliveries_job_id", "webhook_deliveries", ["job_id"])


def downgrade() -> None:
    op.drop_index("ix_webhook_deliveries_job_id", table_name="webhook_deliveries")
    op.drop_table("webhook_deliveries")
    op.drop_column("job_devices", "day0_finished_at")
    op.drop_column("job_devices", "day0_started_at")
    op.drop_column("job_devices", "error")
    op.drop_column("jobs", "day0_image_id")
    op.drop_column("jobs", "day0_config_id")

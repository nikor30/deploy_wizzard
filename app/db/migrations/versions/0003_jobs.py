"""jobs and job_devices tables

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-16

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("current_step", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "job_devices",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("serial", sa.String(length=64), nullable=False),
        sa.Column("pid", sa.String(length=64), nullable=True),
        sa.Column("ccc_device_id", sa.String(length=64), nullable=False),
        sa.Column("match_status", sa.String(length=16), nullable=True),
        sa.Column("netbox_device_id", sa.Integer(), nullable=True),
        sa.Column("netbox_name", sa.String(length=256), nullable=True),
        sa.Column("netbox_site_id", sa.Integer(), nullable=True),
        sa.Column("netbox_site_name", sa.String(length=256), nullable=True),
        sa.Column("ccc_site_id", sa.String(length=64), nullable=True),
        sa.Column("ccc_site_name", sa.String(length=512), nullable=True),
        sa.Column("mgmt_ip", sa.String(length=64), nullable=True),
        sa.Column("mgmt_vlan", sa.Integer(), nullable=True),
        sa.Column("vlan_options", sa.JSON(), nullable=True),
        sa.Column("state", sa.String(length=24), nullable=False),
    )
    op.create_index("ix_job_devices_job_id", "job_devices", ["job_id"])


def downgrade() -> None:
    op.drop_index("ix_job_devices_job_id", table_name="job_devices")
    op.drop_table("job_devices")
    op.drop_table("jobs")

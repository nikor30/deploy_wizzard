"""log entries table + day-n timing columns

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-17

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "job_devices", sa.Column("dayn_started_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "job_devices", sa.Column("dayn_finished_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_table(
        "log_entries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("level", sa.String(length=16), nullable=False),
        sa.Column("component", sa.String(length=128), nullable=False),
        sa.Column("message", sa.String(length=4096), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=True),
        sa.Column("device_serial", sa.String(length=64), nullable=True),
        sa.Column("context", sa.JSON(), nullable=True),
    )
    for column in ("timestamp", "level", "component", "job_id", "device_serial"):
        op.create_index(f"ix_log_entries_{column}", "log_entries", [column])


def downgrade() -> None:
    for column in ("timestamp", "level", "component", "job_id", "device_serial"):
        op.drop_index(f"ix_log_entries_{column}", table_name="log_entries")
    op.drop_table("log_entries")
    op.drop_column("job_devices", "dayn_finished_at")
    op.drop_column("job_devices", "dayn_started_at")

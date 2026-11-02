"""Persistent AI creation tasks."""

import sqlalchemy as sa

from alembic import op

revision = "0026_creation_run"
down_revision = "0025_frame_workspace"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "creation_run",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("state_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("updated_at", sa.String(40), nullable=False),
    )


def downgrade():
    op.drop_table("creation_run")

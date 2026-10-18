"""add separate R2V and FL2VA project presets"""

import sqlalchemy as sa

from alembic import op

revision = "0011_project_video_presets"
down_revision = "0010_project_preset"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("project") as batch_op:
        batch_op.add_column(sa.Column("r2v_preset_name", sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column("flf_preset_name", sa.String(length=100), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("project") as batch_op:
        batch_op.drop_column("flf_preset_name")
        batch_op.drop_column("r2v_preset_name")

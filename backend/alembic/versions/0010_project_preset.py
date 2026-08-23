"""add project preset selection"""

from alembic import op
import sqlalchemy as sa

revision = "0010_project_preset"
down_revision = "0009_project_generation_mode"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("project") as batch_op:
        batch_op.add_column(sa.Column("preset_name", sa.String(length=100), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("project") as batch_op:
        batch_op.drop_column("preset_name")

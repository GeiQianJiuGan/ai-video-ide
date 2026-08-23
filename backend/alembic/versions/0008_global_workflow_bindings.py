"""project capability bindings for the application-level workflow registry."""

from alembic import op
import sqlalchemy as sa

revision = "0008_global_workflow_bindings"
down_revision = "0007_timeline_audio"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("project") as batch_op:
        batch_op.add_column(sa.Column("default_first_last_workflow_id", sa.String(length=40), nullable=True))
        batch_op.add_column(sa.Column("default_upscale_workflow_id", sa.String(length=40), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("project") as batch_op:
        batch_op.drop_column("default_upscale_workflow_id")
        batch_op.drop_column("default_first_last_workflow_id")

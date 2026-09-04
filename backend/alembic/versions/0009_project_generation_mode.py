"""add project-level generation mode"""

from alembic import op
import sqlalchemy as sa

revision = "0009_project_generation_mode"
down_revision = "0008_global_workflow_bindings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("project") as batch_op:
        batch_op.add_column(
            sa.Column(
                "generation_mode",
                sa.String(length=20),
                nullable=False,
                server_default="comfy_preset",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("project") as batch_op:
        batch_op.drop_column("generation_mode")

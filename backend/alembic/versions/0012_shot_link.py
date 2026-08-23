"""add shot_link: per-shot transitions inside a scene

镜头之间也要能配「无转场 / 补一段转场」。没有行 = 硬切，所以老工程升上来行为不变。
"""

import sqlalchemy as sa

from alembic import op

revision = "0012_shot_link"
down_revision = "0011_project_video_presets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "shot_link",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("from_shot_id", sa.String(length=40), nullable=False),
        sa.Column("to_shot_id", sa.String(length=40), nullable=False),
        sa.Column("mode", sa.String(length=20), nullable=False, server_default="cut"),
        sa.Column("shot_id", sa.String(length=40), nullable=True),
        sa.Column("duration", sa.Float(), nullable=False, server_default="1.5"),
        sa.Column("prompt", sa.Text(), nullable=True),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.Column("updated_at", sa.String(length=40), nullable=False),
        sa.ForeignKeyConstraint(["from_shot_id"], ["shot.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["to_shot_id"], ["shot.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("from_shot_id", "to_shot_id", name="uq_shot_link_pair"),
    )
    op.create_index("ix_shot_link_from_shot_id", "shot_link", ["from_shot_id"])
    op.create_index("ix_shot_link_to_shot_id", "shot_link", ["to_shot_id"])


def downgrade() -> None:
    op.drop_index("ix_shot_link_to_shot_id", table_name="shot_link")
    op.drop_index("ix_shot_link_from_shot_id", table_name="shot_link")
    op.drop_table("shot_link")

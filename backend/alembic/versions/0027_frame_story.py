"""帧图的叙事用途与对应剧情。"""

import sqlalchemy as sa

from alembic import op

revision = "0027_frame_story"
down_revision = "0026_creation_run"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("frame_card", sa.Column("story", sa.Text(), nullable=False, server_default=""))
    op.add_column(
        "frame_card", sa.Column("role", sa.String(20), nullable=False, server_default="key")
    )


def downgrade():
    op.drop_column("frame_card", "role")
    op.drop_column("frame_card", "story")

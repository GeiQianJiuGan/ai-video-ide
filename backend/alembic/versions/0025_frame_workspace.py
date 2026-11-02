"""Add independent frame authoring without changing legacy Shot rows."""

import sqlalchemy as sa

from alembic import op

revision = "0025_frame_workspace"
down_revision = "0024_director_intent"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "frame_workspace",
        sa.Column(
            "shot_id", sa.String(40), sa.ForeignKey("shot.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("video_json", sa.Text(), nullable=False),
    )
    op.create_table(
        "frame_card",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column(
            "shot_id", sa.String(40), sa.ForeignKey("shot.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("refs_json", sa.Text(), nullable=False),
        sa.Column("adopted_asset_id", sa.String(40)),
    )
    op.create_index("ix_frame_card_shot_id", "frame_card", ["shot_id"])
    op.create_table(
        "frame_candidate",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column(
            "card_id",
            sa.String(40),
            sa.ForeignKey("frame_card.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("asset_id", sa.String(40), nullable=False),
        sa.Column("job_id", sa.String(40)),
        sa.Column("snapshot_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
    )
    op.create_index("ix_frame_candidate_card_id", "frame_candidate", ["card_id"])


def downgrade():
    for table in ("frame_candidate", "frame_card", "frame_workspace"):
        op.drop_table(table)

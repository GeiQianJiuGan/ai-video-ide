"""工程容器：project 表。

对应 schema_version = 1。之后每加一个迁移，都要在
app/services/projects.py 的 REVISION_SCHEMA 里登记它带来的 schema 版本，
否则打开旧工程时无法显示「schema X → Y」。

Revision ID: 0001_project
Revises: None
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0001_project"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("synopsis", sa.Text()),
        sa.Column("cover_asset_id", sa.String(40)),
        sa.Column("style_preset", sa.String(100)),
        sa.Column("width", sa.Integer(), nullable=False, server_default="1920"),
        sa.Column("height", sa.Integer(), nullable=False, server_default="1080"),
        sa.Column("fps", sa.Float(), nullable=False, server_default="25"),
        sa.Column("aspect_ratio", sa.String(20)),
        sa.Column("duration_unit", sa.String(10), nullable=False, server_default="frames"),
        sa.Column("default_video_workflow_id", sa.String(40)),
        sa.Column("default_image_workflow_id", sa.String(40)),
        sa.Column("default_prompt_style", sa.Text()),
        sa.Column("negative_prompt", sa.Text()),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("updated_at", sa.String(40), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("project")

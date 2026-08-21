"""scene_nodes：幕的小节点（prompt / 人物 / 地点）与主视频。

流程图上的一个节点不再只是「一幕的标题 + 缩略图」，它变成一张小图表：

  1. `scene.prompt` —— 唯一必填的小节点；镜头没写自己的 prompt 时由它兜底；
  2. `scene_cast` / `scene_location` —— 人物与地点各自可多选（上限可配置，
     见 `app/services/appsettings.py` 的 `scene.node_limit`）。地点表里
     `index_no == 0` 的那条同步回 `scene.location_variant_id`（主地点），
     于是 Context Resolver 与分镜板一行都不用改；
  3. `scene.main_version_id` —— 从这一幕已生成的视频里采用一条当主视频，
     节点上就地可播。刻意不加外键（与 `shot.current_version_id` 同理）。

schema 版本随之升到 5，并已在 app/persistence/migrate.py 的 REVISION_SCHEMA 登记；
不登记的话，打开旧工程时无法向用户显示「schema 4 → 5」。

Revision ID: 0005_scene_nodes
Revises: 0004_scene_flow
Create Date: 2026-08-21 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005_scene_nodes"
down_revision: str | None = "0004_scene_flow"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("scene", schema=None) as batch_op:
        batch_op.add_column(sa.Column("prompt", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("main_version_id", sa.String(length=40), nullable=True))

    op.create_table(
        "scene_cast",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("scene_id", sa.String(length=40), nullable=False),
        sa.Column("appearance_id", sa.String(length=40), nullable=False),
        sa.Column("index_no", sa.Integer(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["scene_id"], ["scene.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["appearance_id"], ["appearance.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_scene_cast_scene_id", "scene_cast", ["scene_id"])

    op.create_table(
        "scene_location",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("scene_id", sa.String(length=40), nullable=False),
        sa.Column("location_variant_id", sa.String(length=40), nullable=False),
        sa.Column("index_no", sa.Integer(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["scene_id"], ["scene.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["location_variant_id"], ["location_variant.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_scene_location_scene_id", "scene_location", ["scene_id"])

    # 老工程里已经选过主地点的幕：把它补成 scene_location 的第一条，
    # 免得升级之后界面上「地点小节点」凭空变空了。
    op.execute(
        """
        INSERT INTO scene_location (id, scene_id, location_variant_id, index_no, note)
        SELECT 'scl_' || upper(hex(randomblob(13))), id, location_variant_id, 0, NULL
        FROM scene
        WHERE location_variant_id IS NOT NULL
        """
    )


def downgrade() -> None:
    op.drop_index("ix_scene_location_scene_id", table_name="scene_location")
    op.drop_table("scene_location")
    op.drop_index("ix_scene_cast_scene_id", table_name="scene_cast")
    op.drop_table("scene_cast")
    with op.batch_alter_table("scene", schema=None) as batch_op:
        batch_op.drop_column("main_version_id")
        batch_op.drop_column("prompt")

"""scene_flow：场景衔接（scene_link）、镜头种类（shot.kind）、AI 导演会话（director_turn）。

这一版把生成层从「Workflow 为中心」换成「两级场景系统」，需要三样落盘的东西：

  1. `scene_link` —— 两幕之间怎么接（硬切 / 转场 / 续接末帧）变成可查询的一等公民；
  2. `shot.kind` —— 区分导演排的戏和衔接生成出来的那段转场（默认 `"shot"`，老数据不受影响）；
  3. `director_turn` —— AI 协作栏的对话与提案，刷新页面不丢。

schema 版本随之升到 4，并已在 app/persistence/migrate.py 的 REVISION_SCHEMA 登记；
不登记的话，打开旧工程时无法向用户显示「schema 3 → 4」。

Revision ID: 0004_scene_flow
Revises: 0003_library_origin
Create Date: 2026-08-21 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004_scene_flow"
down_revision: str | None = "0003_library_origin"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "scene_link",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("from_scene_id", sa.String(length=40), nullable=False),
        sa.Column("to_scene_id", sa.String(length=40), nullable=False),
        sa.Column("mode", sa.String(length=20), nullable=False),
        sa.Column("shot_id", sa.String(length=40), nullable=True),
        sa.Column("duration", sa.Float(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=True),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.Column("updated_at", sa.String(length=40), nullable=False),
        sa.ForeignKeyConstraint(["from_scene_id"], ["scene.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["to_scene_id"], ["scene.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("from_scene_id", "to_scene_id", name="uq_scene_link_pair"),
    )
    op.create_index("ix_scene_link_from_scene_id", "scene_link", ["from_scene_id"])
    op.create_index("ix_scene_link_to_scene_id", "scene_link", ["to_scene_id"])

    op.create_table(
        "director_turn",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("content_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    # 老工程里的镜头全是导演排的戏，所以给一个 server_default 一次性填上。
    with op.batch_alter_table("shot", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "kind", sa.String(length=20), nullable=False, server_default=sa.text("'shot'")
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("shot", schema=None) as batch_op:
        batch_op.drop_column("kind")
    op.drop_table("director_turn")
    op.drop_index("ix_scene_link_to_scene_id", table_name="scene_link")
    op.drop_index("ix_scene_link_from_scene_id", table_name="scene_link")
    op.drop_table("scene_link")

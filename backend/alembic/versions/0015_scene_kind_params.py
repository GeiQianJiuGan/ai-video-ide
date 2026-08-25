"""scene_kind_params：幕的来源类型 + 幕级共用参数。

两件事，都是为了让「长视频」不必另建一套体系：

  · **`Scene.kind`**（`storyboard` / `ingested`）——一幕是剧本拆出来的，还是从一段成片切出来的。
    `Scene` 上压根没有 `story_id`（剧本只是工程级的一行原文），所以剧本与分镜在数据层
    早就是解耦的；真正的耦合只有「一幕必须有 prompt」这个隐含假设。有了这一列，
    「这一幕要什么才算完整」就能查表（`services/story.py::SCENE_REQUIRED`）而不是散落
    在三四处 `if`。
  · **`Scene.params_json` + `Scene.param_mode`**——幕级共用参数（negative / duration /
    preset / seed 策略…）与新建镜头时要不要预填。**参数共用不由 kind 决定**，只由
    「镜头上那一项空不空」决定（`services/params.py`），`param_mode` 只影响创建那一刻
    写不写实，绝不在解析路径上分叉。

老工程升上来：所有既有幕都是 `storyboard`、`param_mode='per_shot'`、没有幕级参数，
行为一字不变。

schema 版本随之升到 15，并已在 app/persistence/migrate.py 的 REVISION_SCHEMA 登记。

Revision ID: 0015_scene_kind_params
Revises: 0014_clip_origin
Create Date: 2026-08-25 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0015_scene_kind_params"
down_revision: str | None = "0014_clip_origin"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("scene", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("kind", sa.String(length=20), nullable=False, server_default="storyboard")
        )
        batch_op.add_column(
            sa.Column(
                "param_mode", sa.String(length=20), nullable=False, server_default="per_shot"
            )
        )
        batch_op.add_column(sa.Column("params_json", sa.Text(), nullable=True))
        # 导入幕的镜头区间都指向同一个源文件，这一列记的就是那个文件（可空、不加外键：
        # 资产被删掉时按「源文件已不在」处理，而不是让删除被外键挡下来）。
        batch_op.add_column(sa.Column("source_asset_id", sa.String(length=40), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("scene", schema=None) as batch_op:
        batch_op.drop_column("source_asset_id")
        batch_op.drop_column("params_json")
        batch_op.drop_column("param_mode")
        batch_op.drop_column("kind")

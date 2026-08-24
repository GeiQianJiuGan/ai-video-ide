"""shot_frames：镜头上显式的首帧 / 末帧槽位。

**「哪一张是首帧」得是用户按下去的那一下。** 以前镜头上没有这个字段，于是上下文账单
只好把优先级最高的那一条（通常是角色表）自动提拔成首帧：界面上给一张三视图标了「首帧」，
模型端也真把它当画面第一格用。首尾帧决定「画面从哪一格开始 / 结束」，参考素材决定
「谁出场、在哪儿」——两件事，两处表达，所以镜头上补两列。

两列都可空，**空 = 没有指定**：`use_prev_frame` 的镜头照旧用上游末帧
（`prev_shot_id` + `services/frames.py` 抽出来的那张），否则这个镜头就是没有首帧，
账单照实说。所以老工程升上来只是多了两个空列，行为不变——唯一的变化是账单不再偷偷
提拔参考图（那半边改在 `services/context.py::_assign_roles`，不需要迁移）。

刻意不加外键：与 `shot.current_version_id` 同理（资产表反过来引用不到镜头），
资产被删掉时按「没有首帧」处理，而不是让删除操作被外键挡下来。

schema 版本随之升到 13，并已在 app/persistence/migrate.py 的 REVISION_SCHEMA 登记；
不登记的话，打开旧工程时无法向用户显示「schema 12 → 13」。

Revision ID: 0013_shot_frames
Revises: 0012_shot_link
Create Date: 2026-08-24 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0013_shot_frames"
down_revision: str | None = "0012_shot_link"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("shot", schema=None) as batch_op:
        batch_op.add_column(sa.Column("first_frame_asset_id", sa.String(length=40), nullable=True))
        batch_op.add_column(sa.Column("last_frame_asset_id", sa.String(length=40), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("shot", schema=None) as batch_op:
        batch_op.drop_column("last_frame_asset_id")
        batch_op.drop_column("first_frame_asset_id")

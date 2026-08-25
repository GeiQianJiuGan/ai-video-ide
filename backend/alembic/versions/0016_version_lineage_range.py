"""version_lineage_range：版本的血缘 + 区间。

两列都是为了让「二次处理」和「长视频切段」不必另建一套版本体系——它们产出的仍然是
**同一个镜头上的一个新版本**，于是硬约束 3（只增不改、随时回退）、采用入口、时间线装配、
队列、WS 事件全都一行不用改。

  · **`parent_version_id`**——这一版是从哪一版处理出来的（超分 / 插帧 / 换音频 / 重做尾段）。
    版本轨于是能显示「原始 v1 → 超分 v2 → 换音频 v3」这条谱系，而不是三条互不相干的版本。
    刻意不加外键：父版本被删（或从别的工程搬来）时按「不知道出处」处理。
  · **`in_point` / `out_point`**——这一版只用源文件的某一段（秒）。两列都空 = 整个文件，
    所以老版本行为不变。长视频切段靠它：N 个镜头各挂一版，**asset_id 全部指向同一个
    源文件**，各自带自己的区间，零文件复制；`timeline.auto_assemble` 建片段时把它抄进
    `TimelineClip.in_point` / `out_point`（那两列早就有了），导出侧一行不用改。

schema 版本随之升到 16，并已在 app/persistence/migrate.py 的 REVISION_SCHEMA 登记。

Revision ID: 0016_version_lineage_range
Revises: 0015_scene_kind_params
Create Date: 2026-08-25 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0016_version_lineage_range"
down_revision: str | None = "0015_scene_kind_params"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("generation_version", schema=None) as batch_op:
        batch_op.add_column(sa.Column("parent_version_id", sa.String(length=40), nullable=True))
        batch_op.add_column(sa.Column("in_point", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("out_point", sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("generation_version", schema=None) as batch_op:
        batch_op.drop_column("out_point")
        batch_op.drop_column("in_point")
        batch_op.drop_column("parent_version_id")

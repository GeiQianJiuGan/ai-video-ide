"""shot_adopted_video：把「采用哪一段成片」收回镜头级，去掉 scene.main_version_id。

一幕下面有很多镜头，每个镜头各自独立生成很多段视频，「用哪一段」只能一个镜头一个镜头
地定。这件事本来就有一个指针 `shot.current_version_id`——时间线装配
（`services/timeline.py::auto_assemble`）、下游镜头抽末帧认的都是它。幕上再存一个
`scene.main_version_id` 只会让流程图上播的那一段和导出的那一段各说一套（镜头搬去别的幕、
换了当前版本，那个指针立刻发霉）。所以这一列删掉，采用只走
`POST /projects/{pid}/versions/{version_id}/current`。

删之前**先把用户的选择搬过去**：老工程里如果某一幕采用过主视频，把它落成所属镜头的
当前版本再删列（`adopt_main_video` 当年就是同步写这两处的，这一步通常是幂等的，
但绝不能靠「通常」——静默丢掉用户按过的那一下不可接受）。

另一半改动（抽出来的首尾帧不再算资产、成片删掉时连带删帧）不需要迁移：帧的类型标记
一直是 `asset.kind == "frame"`，落盘目录改成 `cache/frames/` 只影响**新抽**的那些；
老工程里已经在 `assets/frames/` 的那些帧路径记在库里，照旧能读，也照旧被资产页排除。

schema 版本随之升到 6，并已在 app/persistence/migrate.py 的 REVISION_SCHEMA 登记；
不登记的话，打开旧工程时无法向用户显示「schema 5 → 6」。

Revision ID: 0006_shot_adopted_video
Revises: 0005_scene_nodes
Create Date: 2026-08-22 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006_shot_adopted_video"
down_revision: str | None = "0005_scene_nodes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 采用过主视频的那些幕 → 它属于哪个镜头。删列之前用它把选择搬到镜头上。
_ADOPTED = """
    SELECT s.main_version_id FROM scene s
    JOIN generation_version gv ON gv.id = s.main_version_id
    WHERE gv.shot_id = shot.id
"""


def upgrade() -> None:
    op.execute(
        f"""
        UPDATE shot SET current_version_id = ({_ADOPTED})
        WHERE EXISTS ({_ADOPTED})
        """
    )
    with op.batch_alter_table("scene", schema=None) as batch_op:
        batch_op.drop_column("main_version_id")


def downgrade() -> None:
    # 回退只恢复这一列的存在。哪一幕采用过哪一段已经无从得知（镜头级的当前版本
    # 不能反推成幕级的选择），所以一律留空，而不是猜一个填进去。
    with op.batch_alter_table("scene", schema=None) as batch_op:
        batch_op.add_column(sa.Column("main_version_id", sa.String(length=40), nullable=True))

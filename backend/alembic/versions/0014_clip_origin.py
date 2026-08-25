"""clip_origin：时间线片段的来源，装配只碰自己铺的那些。

以前 `auto_assemble(replace=True)` 会把整条视频轨清空重建。只要时间线上还有别的来源的
片段（用户自己加的、从长视频切出来的），下一次装配就会连着用户所有手工裁切 / 静音 /
音量一起抹掉；被拆到音频轨上的声音也会因为源片段 id 消失而全部变成悬空
（`_shape` 里那个 `source_missing` 就是这个现场）。

所以片段要能说出自己是谁铺的：

  · `assembled`——`auto_assemble` 按 Scene / Shot 顺序铺的，它有权更新与删除；
  · `manual`——用户自己添加、拆出来的声音、空白占位，**装配永不触碰**。

回填规则照「行为不变」来定：同时有 `shot_id` 与 `version_id` 的才是装配铺的，
其余（手动添加的素材、空白片段、拆出来的音频）一律 `manual`。

schema 版本随之升到 14，并已在 app/persistence/migrate.py 的 REVISION_SCHEMA 登记。

Revision ID: 0014_clip_origin
Revises: 0013_shot_frames
Create Date: 2026-08-25 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0014_clip_origin"
down_revision: str | None = "0013_shot_frames"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("timeline_clip", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "origin",
                sa.String(length=20),
                nullable=False,
                server_default="manual",
            )
        )
    # 老工程里「装配铺的」= 同时挂着镜头与版本的那些。先全置 manual 再挑出来改，
    # 这样任何认不出来的片段都落在「装配不碰」这一侧——宁可少管，不可误删。
    op.execute(
        "UPDATE timeline_clip SET origin = 'assembled' "
        "WHERE shot_id IS NOT NULL AND version_id IS NOT NULL"
    )


def downgrade() -> None:
    with op.batch_alter_table("timeline_clip", schema=None) as batch_op:
        batch_op.drop_column("origin")

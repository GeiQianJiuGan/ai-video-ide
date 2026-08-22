"""timeline_audio：片段上补 muted / volume / source_clip_id，音频轨才能真的叠起来。

「把某一块视频的声音拆出来成为独立的音频轨」这件事只加了三列，但每一列都有它必须存在
的理由：

  · `muted`——声音一挪到音频轨上，源片段就必须闭嘴，否则同一段声音会被听见两遍
    （画面自带一份 + 音频轨一份）。轨道级的 `track.muted` 管不了这件事：那是整条轨道
    的开关，而拆出来的只是其中某一段；
  · `volume`——叠加才是音频轨的意义所在。两条轨道混在一起时，「哪一条大一点」是唯一
    真正要调的东西，混音时它直接变成 filter 里的 `volume=`；
  · `source_clip_id`——记住这段音频是从哪一段画面拆出来的。**刻意不加外键**：源片段
    被删掉或者重新装配之后，这段声音照旧能播、照旧能导出，只是不再知道出处
    （界面上标成「来源片段已不在」）。加了外键就会变成「删画面顺手删声音」，
    那是静默丢用户数据。

`volume` 默认 1（原样）、`muted` 默认 0，所以老工程升上来行为完全不变：没拆过声音的
时间线导出结果与从前一致。

schema 版本随之升到 7，并已在 app/persistence/migrate.py 的 REVISION_SCHEMA 登记；
不登记的话，打开旧工程时无法向用户显示「schema 6 → 7」。

Revision ID: 0007_timeline_audio
Revises: 0006_shot_adopted_video
Create Date: 2026-08-22 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007_timeline_audio"
down_revision: str | None = "0006_shot_adopted_video"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("timeline_clip", schema=None) as batch_op:
        batch_op.add_column(sa.Column("muted", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("volume", sa.Float(), nullable=False, server_default="1.0"))
        batch_op.add_column(sa.Column("source_clip_id", sa.String(length=40), nullable=True))
        batch_op.create_index("ix_timeline_clip_source_clip_id", ["source_clip_id"], unique=False)


def downgrade() -> None:
    # 回退会丢掉「哪一段被静音、哪一段是从哪儿拆出来的」。音频轨上的片段本身还在
    # （它们是普通片段），只是回退后与源片段的关系断了，源片段也会重新出声。
    with op.batch_alter_table("timeline_clip", schema=None) as batch_op:
        batch_op.drop_index("ix_timeline_clip_source_clip_id")
        batch_op.drop_column("source_clip_id")
        batch_op.drop_column("volume")
        batch_op.drop_column("muted")

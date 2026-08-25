"""shot_dialogue：镜头上的台词（音源那条链要它）。

音源服务要回答的是「**说什么** / 什么声音」，而在这之前分镜上压根没有一个地方放台词——
`description` 是画面描述（「中景，她转身推开门」），`prompt` 是喂给视频模型的那句话。
把台词塞进它们任何一个，出来的画面里都会多出念稿子的字幕感，而音源那边又拿不到干净的文本。

所以单独一列，`Shot.dialogue`：
  · **可空**，空 = 这个镜头不说话（只出环境音 / 音乐的镜头很常见，那时靠一句声音描述即可，
    见 `services/dub.py::text_of`）；
  · **不进 prompt**：视频那条链一个字都不读它，加这一列不改变任何已有生成行为；
  · 幕级兜底也在这里（`scene.dialogue`）：长视频切出来的镜头本来没有台词，整幕配同一段
    旁白是常态，一个个填是折磨。

老工程升上来只是多两个空列，行为不变。schema 版本随之升到 18，并已在
app/persistence/migrate.py 的 REVISION_SCHEMA 登记。

Revision ID: 0018_shot_dialogue
Revises: 0017_shot_audio_version
Create Date: 2026-08-25 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0018_shot_dialogue"
down_revision: str | None = "0017_shot_audio_version"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("shot", schema=None) as batch_op:
        batch_op.add_column(sa.Column("dialogue", sa.Text(), nullable=True))
    with op.batch_alter_table("scene", schema=None) as batch_op:
        batch_op.add_column(sa.Column("dialogue", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("scene", schema=None) as batch_op:
        batch_op.drop_column("dialogue")
    with op.batch_alter_table("shot", schema=None) as batch_op:
        batch_op.drop_column("dialogue")

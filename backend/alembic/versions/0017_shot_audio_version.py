"""shot_audio_version：镜头上「当前采用的音频版本」。

AI 生成的视频那条音轨质量往往很差，而想换掉它以前只能**重跑整个视频**——画面明明是好的。
所以声音要能独立成一条生成链：音频版本仍然是同一张 `generation_version` 表里
`kind="audio"` 的行（只增不改、可回退、冻结参数全部复用），但「用哪一版」必须是**第二个
指针**——`shot.current_version_id` 是画面那一版，`timeline.auto_assemble` 装配的、下游
镜头抽末帧认的都是它，两件事共用一个指针必然打架。

装配时的规则（`services/timeline.py`）：镜头有当前音频版本 → 视频片段置 `muted=1`，
音频轨上放这一版。于是**换音频不再触碰画面版本，一个字节都不用重跑**。

可空、不加外键：与 `current_version_id` 同理，版本行不在了按「没有采用音频」处理。
老工程升上来只是多一个空列，行为不变。

schema 版本随之升到 17，并已在 app/persistence/migrate.py 的 REVISION_SCHEMA 登记。

Revision ID: 0017_shot_audio_version
Revises: 0016_version_lineage_range
Create Date: 2026-08-25 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0017_shot_audio_version"
down_revision: str | None = "0016_version_lineage_range"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("shot", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("current_audio_version_id", sa.String(length=40), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("shot", schema=None) as batch_op:
        batch_op.drop_column("current_audio_version_id")

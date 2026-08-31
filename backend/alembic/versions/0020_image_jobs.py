"""image_jobs：出图任务进同一个队列——`job.shot_id` 改可空 + `target_kind` / `target_id`。

角色四视图 / 地点参考图 / 道具图**不属于任何镜头**（它们是素材，被很多镜头引用），
可 `job.shot_id` 一直是 `nullable=False`。于是出图要么另建一张队列表，要么随便挑一个
镜头挂上去——前者意味着另一套取消 / 重试 / 优先级 / 进度事件（同一件事两份实现，
迟早行为分叉），后者会让「这个镜头的任务」列表里冒出一条与它无关的出图任务。

所以这一列改可空，另加两列说清「这张图是给谁出的」：
`target_kind` ∈ appearance / location_variant / prop / shot_first_frame / shot_last_frame，
`target_id` 是那一行的 id（**不加外键**：素材被删掉时按「这条任务的落点没了」处理并如实
报错，比让数据库连带删掉一条正在跑的任务更好解释）。

`shot_id` 的外键与 `ondelete="CASCADE"` 原样保留——可空外键不影响它，镜头级任务照旧
跟着镜头一起删。`GenerationVersion` **一列都不动**：素材图的「永不覆盖」由
`SheetVersion` / `LocationReference` / `PropReference` 已有的 `version_no` + `is_current`
保证，出图不需要在镜头上造一个版本。

schema 版本随之升到 20，并已在 app/persistence/migrate.py 的 REVISION_SCHEMA 登记；
不登记的话，打开旧工程时无法向用户显示「schema 19 → 20」。

Revision ID: 0020_image_jobs
Revises: 0019_job_batch
Create Date: 2026-08-31 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0020_image_jobs"
down_revision: str | None = "0019_job_batch"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    #: SQLite 改列必须走 batch 模式（它没有 ALTER COLUMN，alembic 靠重建表实现）。
    with op.batch_alter_table("job", schema=None) as batch_op:
        batch_op.alter_column("shot_id", existing_type=sa.String(length=40), nullable=True)
        batch_op.add_column(sa.Column("target_kind", sa.String(length=30), nullable=True))
        batch_op.add_column(sa.Column("target_id", sa.String(length=40), nullable=True))


def downgrade() -> None:
    #: 回滚时先把出图任务清掉：它们的 `shot_id` 是空的，列改回 NOT NULL 会失败。
    op.execute("DELETE FROM job WHERE shot_id IS NULL")
    with op.batch_alter_table("job", schema=None) as batch_op:
        batch_op.drop_column("target_id")
        batch_op.drop_column("target_kind")
        batch_op.alter_column("shot_id", existing_type=sa.String(length=40), nullable=False)

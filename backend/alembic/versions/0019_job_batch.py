"""job_batch：一次编排入队的那一批任务，在队列里合并成一条。

点一下「单线程续接」会一口气入队几十条任务。队列里逐条平铺时，用户看到的是几十行长得
一模一样的东西，看不出「这是我刚才点的那一下」，更看不出「走到第几个了」——而
ComfyUI 压根不回显进度，那个百分比进度条从头到尾都是编的。

所以任务上补四列，把「一次编排」表达出来：`batch_id` 把成员认回来，`batch_label` 是
入队那一刻定死的名字，`batch_kind` 只用于文案与图标，`batch_seq` 让界面能说
「执行到第 3/12 步」。

三条刻意的规矩：
  · **空值是常态**——单个镜头的生成不属于任何编排，队列里照旧一行一条，老工程升上来
    只是多了四个空列，行为不变；
  · **不另立 batch 表**——一次编排没有任何独立于成员任务的状态（总数、走到第几、
    失败在哪一条全部能算出来），另一张表只会多一份可能对不上的真相；
  · **失败也不清这一列**——「整批重跑」就是靠它把成员找回来的（单线程续接一条失败会
    连带停掉后面全部，重跑必须是一次动作，不是几十次点击）。

schema 版本随之升到 19，并已在 app/persistence/migrate.py 的 REVISION_SCHEMA 登记；
不登记的话，打开旧工程时无法向用户显示「schema 18 → 19」。

Revision ID: 0019_job_batch
Revises: 0018_shot_dialogue
Create Date: 2026-08-27 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0019_job_batch"
down_revision: str | None = "0018_shot_dialogue"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("job", schema=None) as batch_op:
        batch_op.add_column(sa.Column("batch_id", sa.String(length=40), nullable=True))
        batch_op.add_column(sa.Column("batch_label", sa.String(length=200), nullable=True))
        batch_op.add_column(sa.Column("batch_kind", sa.String(length=30), nullable=True))
        batch_op.add_column(sa.Column("batch_seq", sa.Integer(), nullable=True))
        batch_op.create_index("ix_job_batch_id", ["batch_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("job", schema=None) as batch_op:
        batch_op.drop_index("ix_job_batch_id")
        batch_op.drop_column("batch_seq")
        batch_op.drop_column("batch_kind")
        batch_op.drop_column("batch_label")
        batch_op.drop_column("batch_id")

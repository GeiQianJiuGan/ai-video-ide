"""asset_description：素材能带一句描述——`asset.description` + `character.description`。

用户自己上传的素材多数没有一句描述，而「引用一个素材」最终只变成模型看得到的一句话
（`providers/base.py::ref_hint()` 渲染的那句「参考素材说明：参考图1=…」）。`Asset` 表以前只有
`path` / `kind` / `mime` / `meta_json`——**没有任何字段能装「这张图长什么样」**，于是没有描述的
素材等于只递给模型一个文件名，生成视频的 prompt 根本构建不起来。

描述**刻意落一列而不是塞 `meta_json`**：它要能被列表查询（「哪些素材还没有描述」）、
要能单独 PATCH、要进上下文账单被冻结，塞进 JSON 里这三件事都得绕。

`character.description` 一起加：`Location` / `Prop` 本来就有这一列，只有角色缺，
所以 `services/director.py` 的 `add_character` 提案里那句「设定」一直被
`cast.CHARACTER_FIELDS` 静默丢掉。补齐这一列 + 把 `"description"` 加进那张字段表才是一处口径。

两列都可空、**不给默认值也不回填**：空 = 用户没写，与「写了一句空字符串」在语义上没差别，
省掉一次全表 UPDATE。

schema 版本随之升到 21，并已在 app/persistence/migrate.py 的 REVISION_SCHEMA 登记；
不登记的话，打开旧工程时无法向用户显示「schema 20 → 21」。

Revision ID: 0021_asset_description
Revises: 0020_image_jobs
Create Date: 2026-08-31 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0021_asset_description"
down_revision: str | None = "0020_image_jobs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    #: SQLite 加列走 batch 模式（与 0020 同一个理由：它没有完整的 ALTER TABLE）。
    with op.batch_alter_table("asset", schema=None) as batch_op:
        batch_op.add_column(sa.Column("description", sa.Text(), nullable=True))
    with op.batch_alter_table("character", schema=None) as batch_op:
        batch_op.add_column(sa.Column("description", sa.Text(), nullable=True))


def downgrade() -> None:
    #: 回滚会丢掉用户写的描述（列没了，没有别处存）。这一点在两列都可空时无法避免，
    #: 所以 downgrade 只做该做的事：把列去掉，不去猜要不要先搬进 meta_json。
    with op.batch_alter_table("character", schema=None) as batch_op:
        batch_op.drop_column("description")
    with op.batch_alter_table("asset", schema=None) as batch_op:
        batch_op.drop_column("description")

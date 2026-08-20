"""library_origin：character / location / prop 记下「从素材库采用而来」的出处。

只是出处，不是外键——采用是单向复制（services/adopt.py），工程运行期完全不依赖
素材库在不在。素材文件的出处走 Asset.meta_json，不占列。

schema 版本随之升到 3，并已在 app/persistence/migrate.py 的 REVISION_SCHEMA 登记；
不登记的话，打开旧工程时无法向用户显示「schema 2 → 3」。

Revision ID: 0003_library_origin
Revises: 0002_domain
Create Date: 2026-08-20 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_library_origin"
down_revision: str | None = "0002_domain"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("character", "location", "prop")


def upgrade() -> None:
    for table in TABLES:
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.add_column(sa.Column("origin_library_id", sa.String(length=40), nullable=True))


def downgrade() -> None:
    for table in TABLES:
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_column("origin_library_id")

"""story_screenplay：给 `story` 加一列 `screenplay_md`——AI 维护的剧本 MD 文档。

这是「AI 导演分步写剧本」工作流第一步的落点：AI 根据用户那句话一段一段攒出一份
Markdown 剧本，随对话更新，「类似 AI 记忆，保持在项目里」。它和 `story.raw_text`
是两件事——`raw_text` 是用户贴进来的原文（源头，绝不悄悄覆盖），`screenplay_md`
是 AI 攒出来的那份底本，拆幕 / 拆镜头都以它为准。

只多一列、可空、默认空串（老工程升上来就是「还没开始攒」，行为不变）。`shot` / `scene`
一列都不动：镜头的「剧情详情」复用已有的 `shot.description`（界面上本来就叫这个名字），
最终提示词照旧是 `shot.prompt`——这一轮不新增镜头字段。

schema 版本随之升到 23，并已在 app/persistence/migrate.py 的 REVISION_SCHEMA 登记；
不登记的话，打开旧工程时无法向用户显示「schema 22 → 23」。

Revision ID: 0023_story_screenplay
Revises: 0022_project_route
Create Date: 2026-09-19 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0023_story_screenplay"
down_revision: str | None = "0022_project_route"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    #: 非空列必须带 server_default，否则已有的那一行 story 升级时会因为 NULL 违反约束。
    op.add_column(
        "story",
        sa.Column("screenplay_md", sa.Text(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("story", "screenplay_md")

"""director_intent：把 AI 导演从「产 H3 散文的翻译器」拆成两层，为这件事各加一列。

这一轮的核心是**创作层与规范层分开**（把硬约束 1「业务层不绑具体视频模型」延伸到 prompt 层）：

  - `shot.intent_json`——**模型无关的「导演意图」**（剧情 / 动作 / 景别 / 视角 / 运镜 /
    起始画面 / 末帧定格 / 对白 / 出场主体 / 时长 / 无配乐标记）。AI 导演写工具产它、落进这一列；
    提交时由生成层按所选 skill 的渲染器翻成该模型的 prompt 形状。可空——Manual 模式不填意图、
    照旧直接写 `shot.prompt`（硬约束 2 不变），老镜头升上来也只是多一个空列，渲染器回退到
    现有的 `shot.prompt` / 四段解析，**输出与升级前逐字相同**。
  - `project.render_skill`——**这个工程照哪份 SKILL 渲染**（默认走设置页 `video.skill`）。
    做成工程级可继承的一列：空串 `''` = 跟随设置页（绝大多数工程），显式选了就不再跟，
    与 `project.generation_mode`（0022）同一套语义。ComfyUI 预设路根本不知道用户跑的是什么
    模型，所以 skill 必须显式选、不能从 provider 推。

`job` / `generation_version` 一列都不动：意图与 skill 是**入队时冻结进
`job.params_json`**（`director_intent` + `route.skill`）的，经 `add_version` 自动进
`GenerationVersion.params_json`——不是新列（硬约束 3，版本永不覆盖）。

schema 版本随之升到 24，并已在 app/persistence/migrate.py 的 REVISION_SCHEMA 登记；
不登记的话，打开旧工程时无法向用户显示「schema 23 → 24」。

Revision ID: 0024_director_intent
Revises: 0023_story_screenplay
Create Date: 2026-09-24 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0024_director_intent"
down_revision: str | None = "0023_story_screenplay"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    #: 意图可空：Manual 模式与老镜头没有意图，渲染器回退到 `shot.prompt`。
    op.add_column("shot", sa.Column("intent_json", sa.Text(), nullable=True))
    #: 可空、默认空串——空串就是「跟随设置页 video.skill」（同 project.generation_mode）。
    op.add_column(
        "project",
        sa.Column("render_skill", sa.String(length=40), nullable=True, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("project", "render_skill")
    op.drop_column("shot", "intent_json")

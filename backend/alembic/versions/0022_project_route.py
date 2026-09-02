"""project_route：`generation_mode` 从「只写的死值」变成**工程级可继承的路由**。

这一列在库里从 `0009_project_generation_mode` 就存在，但**到今天为止从来没有被读过**：
`GenerationService.enqueue_shot` 里那一行是 `generation_mode = "comfy_preset"` 写死的，
执行时也是 `registry.provider("comfy_preset")` 写死的。于是界面上选了「通用 REST API」
或「ComfyUI 工作流绑定」，后端照旧提交给 ComfyUI 预设——选了等于没选。

改成真的会被读之后，这一列需要能表达第三种状态：

  · `"comfy_preset"` / `"http_api"` / `"comfy_workflow"` —— 这个工程**显式**走这条路；
  · `""`（空串）—— **跟随设置页**（应用级 `video.provider`）。绝大多数工程是这一种。

所以这里做三件事：

  1. 列改可空（SQLite 没有 ALTER COLUMN，必须走 `batch_alter_table` 重建表）；
  2. `workflow_api` → `comfy_workflow`：**同一条路两个名字**是历史遗留（项目列与
     `api/workflows.py` 用前者，registry 与设置页用后者，中间没有任何映射）。归一到
     registry 那个名字，读写两侧再各过一次 `route.normalize()` 收老客户端；
  3. 把等于默认值的老行（`comfy_preset` / NULL）清成空串。**这不丢用户意图**——这一列
     从来没被读过，`comfy_preset` 是建工程时无条件写进去的，不是谁选的；清成「继承」
     之后，行为与升级前逐字相同（设置页默认就是 `comfy_preset`），但改了设置页之后
     老工程会跟着变，这才是用户预期。

`Job` / `GenerationVersion` **一列都不动**：路由是**入队时解析一次并冻结进
`job.params_json["route"]`** 的（照硬约束 3 的作风），执行时只读冻结值。给它们加列
只会多出一份会和 `params` 打架的真源。

schema 版本随之升到 22，并已在 app/persistence/migrate.py 的 REVISION_SCHEMA 登记；
不登记的话，打开旧工程时无法向用户显示「schema 21 → 22」。

Revision ID: 0022_project_route
Revises: 0021_asset_description
Create Date: 2026-09-02 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0022_project_route"
down_revision: str | None = "0021_asset_description"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    #: SQLite 改列必须走 batch 模式（它没有 ALTER COLUMN，alembic 靠重建表实现）。
    with op.batch_alter_table("project", schema=None) as batch_op:
        batch_op.alter_column(
            "generation_mode", existing_type=sa.String(length=20), nullable=True
        )
    op.execute("UPDATE project SET generation_mode='comfy_workflow' WHERE generation_mode='workflow_api'")
    op.execute(
        "UPDATE project SET generation_mode='' "
        "WHERE generation_mode IS NULL OR generation_mode='comfy_preset'"
    )


def downgrade() -> None:
    #: 回滚时先把「继承」填回旧的默认值，否则列改回 NOT NULL 会失败。
    #: `comfy_workflow` 换回老名字，旧代码只认那个。
    op.execute("UPDATE project SET generation_mode='comfy_preset' WHERE generation_mode IS NULL OR generation_mode=''")
    op.execute("UPDATE project SET generation_mode='workflow_api' WHERE generation_mode='comfy_workflow'")
    with op.batch_alter_table("project", schema=None) as batch_op:
        batch_op.alter_column(
            "generation_mode",
            existing_type=sa.String(length=20),
            nullable=False,
            server_default="comfy_preset",
        )

# 交接：工程路由（Route）改造

**给下一个会话的人**：这一轮在做的事是 `C:\Users\76763\.claude\plans\workflow-api-workflow-fluttering-quill.md`
那份已批准的计划（A–G 七节）。**后端 A–F 六节已全部落地**，剩下的是**测试、前端（G 节）、文档**。
先读那份计划，再读这里的「已完成」与「还没做」两段。

一句话目标：**「这个工程 + 这个能力 → 走哪条路、这条路要绑什么、绑没绑上、缺什么」只有一份口径**
（`backend/app/services/route.py`）；**入队解析一次并冻结进任务参数，执行时只读冻结值**，重试不重新解析。

## 已完成（后端，已 import 通过）

1. **`backend/app/services/route.py`（新）** —— 唯一口径。
   - `INHERIT = ""`（工程那一列留空 = 跟随设置页）、`ALIAS = {"workflow_api": "comfy_workflow"}`；
     `normalize()` 未知值抛 `VALIDATION_ERROR`（写入侧），`_safe_normalize()` 坏值原样回（只读侧不许 500）。
   - `BINDS = {"comfy_preset": "preset", "http_api": "base_url", "comfy_workflow": "workflow"}`
     —— **三条路的分岔一律查这张表，业务层一个 `if provider ==` 都不写**（硬约束 1）。
   - `Route` 数据类：`provider/label/source/capability/binds_workflow/**binds**/preset/workflow_id/
     workflow_name/base_url/ready/issues`；`to_dict()` 给界面，`frozen()` 给任务参数
     （**刻意不带 `ready`/`issues`/密钥**）。
   - `capability_of(shot, kind)`（唯一实现）、`resolve()`/`capacity()`/`summary()`（**绝不抛**）、
     `require()`（不 ready 就抛 `issues[0]`，这是入队门槛）。
   - `summary(pid)` 一次回两条能力（`image2video` / `first_last_frame`）+ 顶层
     `mode/provider/label/source/binds_workflow/binds/options/settings_provider/contract`，
     每条能力带 `capability_label` 与 `slots`。
2. **迁移 `0022_project_route`** + `models.py`（`generation_mode` 可空、默认 `""`）+
   `migrate.py::REVISION_SCHEMA` + `core/config.py schema_version: 22`。老库里 `workflow_api` 归一，
   等于旧默认值 `comfy_preset` 的行清成「继承」（这一列在此之前从未被读过，不丢用户意图）。
3. **`comfy_workflow` 提成一等适配器**：`app/generation/comfy/graph.py`（`SLOTS`/`parse_graph`/
   `apply_bindings` 下沉，`services/workflows.py` 顶部重新导出，调用方一行未改）、
   `providers/comfy_base.py::ComfyTasks`（`poll`/`fetch`/`_upload`/`_error_detail` 共用）、
   `providers/comfy_workflow.py`（原 `_run_legacy` 的提交逻辑，含「只收图片、跳过的写进 notes」）。
   `registry.py` 拉平（`LEGACY = ()`、`is_legacy` 删除，`listing()` 的 `legacy` 键保留恒 `False`）。
   `providers/base.py` 加 `WorkflowSpec` + `VideoRequest.workflow`（**不塞 `extra`**，图几十 KB 不进冻结参数）。
4. **`services/generation.py`**：`enqueue_shot` 用 `route.capability_of` + `route.require`（入队门槛），
   把 `route.frozen()` 写进 `params["route"]`、`params["generation_mode"]` 第一次是真的；
   `_provider_of(params)` 三级回退（`params.route.provider` → `params.generation_mode` →
   `settings.video_provider`）供老 job 重试；`_run_legacy` 整个删除，`_execute` 不再按 `job.workflow_id` 分支
   （装 `WorkflowSpec` 的条件是「这个任务有绑定的图」这个事实，不是路的名字）。
5. **`services/context.py`**：`project_ref_capacity(pid, capability)` 转调 `route.capacity`；
   `resolve()`/`snapshot()` 多一个 `capability=` 参数（与入队同一算法）。
6. **`api/projects.py`**：`PUT /preset` 里那句 `row.generation_mode = "comfy_preset"` 删掉（改预设不许改路）；
   新增 `GET /projects/{pid}/route` → `route.summary(pid)`。
7. **`services/workflows.py`**：`project_bindings`/`set_project_bindings` 两侧归一，`capability_matrix`
   多回 `route` 块（`_route_block`）；`api/workflows.py` 的 `Literal` 收 `""`/`workflow_api`、默认 `""`；
   `services/packages.py::_env_of` 归一。
8. **`providers/http_api.py`**：合同加 `source_video` + `source_video_name`（`CONTRACT` 与模块头同步）。
9. **`services/overview.py`**：`environment()` 里「怎么出片」那一块整个来自 `route.summary()`
   （新方法 `_generation_of`）、服务探测改 `registry.provider(...).probe()` 包 try/except（新方法 `_probe_of`），
   模块级加 `_bound_of` / `_route_detail`。`comfy`/`capabilities` 两个键与所有旧 `generation.*` 键名原样保留。
10. **`services/refine.py`**（刚完成）：新增 `WAY_OUT_WORKFLOW` 与 `_route_bill(r, kind, override)`
    —— **按 `Route.binds` 分岔**：`preset` 路沿用原来的 `preset_of`/`preset_ready`（二次处理有自己那份
    `settings.refine_preset`，所以刻意不看 `r.ready`）；`base_url` 路 `preset=None`、地址配好就能做
    （不再谎报「还没有选预设」）；`workflow` 路**明确做不了**（绑定表没有源视频槽位）并给出路。
    `plan()` 多回一个 `route` 块（`provider/label/source/binds`），`run()` 的错误标题改成
    「这条路做不了二次处理」并把 `route_frozen`（`provider/label/source/capability`）写进任务参数
    —— 于是 `_run_refine` 提交给**这个工程那条路**，不再是 `settings.video_provider`。
    注意：refine 的 `params["preset"]` 保持原样（二次处理那份预设与出画面那份不是同一个，
    所以 `route_frozen` 里刻意不带 `preset`）。

## 还没做（按优先级）

### 1. 测试（最要紧，后端改动尚未跑过一次完整回归）

```bash
cd backend && .venv/Scripts/python -m pytest -q
```

我在交接前刚起了这条命令，**结果没等到**（输出在
`C:\Users\76763\AppData\Local\Temp\claude\E--01-Work-01-codeSource-aipj-xunjie-video-ide\4041907a-a90d-4e8a-85d1-d285e07312b3\tasks\bip6uzhur.output`，
带 `-x`，第一个失败就停）。**新会话第一件事就是重跑它。** 已知需要跟着改的现存断言：

- `tests/test_providers.py:626` —— `registry.is_legacy` 已删除。
- `tests/test_ref_capacity.py:151–153`、`tests/test_settings_api.py:137–138` —— 槽位现在按解析出来的
  那条路数，期望值要跟着换。
- `tests/test_m3_workflow_story_context.py:~170–195` —— 补两条：PUT `workflow_api` 读出来是
  `comfy_workflow`；`PUT /preset` 之后 `generation_mode` 不变。
- `tests/test_m4_generation_timeline_overview.py:756–790` —— 环境栏 `generation.*` 的键名没变，
  但 `mode` 不再恒等于 `comfy_preset`。

**新增 `tests/test_route.py`**（计划里列的六组）：继承与 `source`（工程空 → settings → 默认）·
`workflow_api` 别名读写两侧归一 · 三条路 readiness 的四要素齐全（用 `conftest.py::error_of`）·
`capacity()` 三条路各一个数（REST 不限量、绑定路视频/音频 = 0）· `capability_of` 对 `prev_shot_id` /
`kind="transition"` 的判定 · **入队门槛**：`POST /queue/pause` 后把工程设成 `http_api` 且不配地址，
断言 `POST /shots/{id}/generate` 直接 422 四要素（而不是排进队列再失败）。
另：`tests/test_providers.py` 补 `ComfyWorkflowProvider.submit`（monkeypatch `ComfyClient`）与
`http_api` 的 `mode="refine"` body 带 `source_video`。

### 2. 前端（G 节，六个文件 + 两处类型）

- `features/project/OverviewView.vue`（387–450 那块）→「这个工程怎么出片」：调用方式 `<select>`
  （第一项 **跟随设置页** = `''`，标出 `mode_source`）→ 照 **`binds`** 画哪一组控件
  （`preset` 两个预设下拉 / `workflow` 四个能力下拉 / `base_url` 一行「这条路不需要工作流绑定：
  首末帧与参考素材整组按 REST 合同发过去；地址在设置页配，密钥不回显」）→ 参考素材槽位三个数 →
  `issues` 的四要素 + `suggestions` 原样列出。数据源：`GET /projects/{pid}/route`（一个请求够了）。
- `features/workflow/WorkflowsView.vue`：顶部横幅显示当前路由与来源；四个能力下拉的 `:disabled`
  从 `generation_mode !== 'workflow_api'` 改成 `!binds_workflow`，**并在旁边写清为什么**（H9 就是这句话缺失）。
- `shared/api/workflows.ts:110` `GenerationMode` 加 `''` 与 `'comfy_workflow'`（去掉 `'workflow_api'`，
  后端读出来已归一）；`stores/workflows.ts:28,84` 默认值改 `''`；`shared/api/projects.ts` 加 `route(pid)`。
- `shared/api/overview.ts` 的 `EnvironmentStatus.generation`：`mode: 'comfy_preset'` 必须放宽，
  并补 `mode_label`/`mode_source`/`binds_workflow`/`workflow_name`/`service`/`ready`/`issues`/`ref_detail`。
- `features/generation/QueueView.vue` 与 `features/flow/SceneWorkbench.vue` 显示 `params.route.label`。
- `features/story/components/RefineModal.vue`：现在无条件显示 `planResult.preset || '默认视频预设'`
  —— 走 REST 时这是在说一个不存在的东西。改成照 `planResult.route.binds === 'preset'` 才显示那一行，
  否则显示 `planResult.route.label` + `preset_detail`。

```bash
cd frontend && npm run typecheck && npm run lint && npm run build
```

### 3. 文档

`docs/03`（`GET /projects/{pid}/route`、0022 列变更）· `docs/05`（三种调用方式那一章改成
「路由 = 工程级可继承」，REST 合同加 `source_video`）· `CLAUDE.md`（「`registry.provider()` 按应用级
设置选」改成「按工程路由解析」，`comfy_workflow` 从「兼容路径」提成第三条正经路，写明
**入队冻结 / 执行只读冻结值**这条规矩；`schema_version` 21 → 22）。

## 别踩的坑

- **循环 import**：`services/route.py` 模块级 import 了 `services/workflows.py`，所以 `workflows.py`
  与 `context.py` 里要用 `route` 只能**函数内延迟 import**；`overview.py` / `refine.py` / `generation.py`
  可以模块级 import（它们没人反向 import）。
- **provider 层不许 import service 层**（`app/generation/providers/*` 只能碰 `app.core.*` /
  `app.generation.*`）——这就是纯函数下沉到 `generation/comfy/graph.py` 的原因。
- **`probe()` 只回答「服务在不在」**，绑没绑上那半句在 `issues` 里。两处各判一遍必然分叉成
  「概览页说就绪、一按生成说没绑图」。
- **重试不重新解析路由**：`require()` 只在入队时挡，已入队的任务用冻结的那一份。
- backend 命令必须用 `.venv/Scripts/python`（全局 python 的 fastapi/starlette 不兼容，import 期就崩）。
- `ruff check .` 在这个仓库**本来就不干净**（`alembic/versions/0008–0011`、`ai/llm/protocols.py`、
  `services/assets.py`、`api/projects.py`、`services/context.py` 都有既存 E501）。这一轮新写的文件里
  只剩 `alembic/versions/0022_project_route.py` 三条 E501，与既存迁移同一种情况。
  **别顺手跑 `ruff format .`**：会把整个仓库重排，混进这次的 diff 里看不出改了什么。

## 端到端验收（计划里的六步，一步都没做过）

`python scripts/dev.py` 起环境后开演示工程：① 调用方式留空 → 显示「跟随设置页 · ComfyUI 预设」，
改成通用 REST API → 变成「不需要工作流绑定」+ 缺地址的四要素，配上地址后 `issues` 清空；
② 按生成 → 队列里路由标签是「通用 REST API」，请求真的打到 REST 而**不再打到 ComfyUI**；
③ 版本参数里 `params.route.provider == "http_api"`、`params.preset` 为空、`ref_notes` 与实际喂的对得上；
④ 换成 ComfyUI 工作流绑定并给 `image2video` 绑一份 ready 的图 → **这条路今天是死代码，改完能真的出片**，
槽位显示「视频 / 音频 0 槽」；⑤ `PUT /preset` 改一次预设，调用方式没被改回 `comfy_preset`；
⑥ 拿 schema 21 的老工程打开 → 提示 `21 → 22`，行为与升级前一致。

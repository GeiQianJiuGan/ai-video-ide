# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

AI Video Studio（`aivs`）：桌面端优先的 AI 原生长视频制作工作台。定位是
**AI = 素材生产器 · System = 视频工程与编排器 · Human = 导演**。
仓库内注释、错误文案、文档一律中文，新代码沿用这一习惯。

## 四条硬约束（改动前先确认没有违反）

1. **业务层不绑定具体视频模型**——不允许出现 `if model == "wan"`；差异全部下沉到 Workflow
   绑定表与 `apply_bindings`（`app/services/workflows.py`）。Shot 只写 capability
   （`text2image` / `image2video` / `first_last_frame` / `upscale`）。整个后端只有
   `GenerationService._execute` 与 `app/generation/comfy/client.py` 知道 ComfyUI 存在。
2. **LLM 不是必选项**——默认 `llm_provider="none"`，AI 入口返回 `LLM_UNAVAILABLE`，且建议里
   必须写明手动路径。Manual 模式必须能走完全流程，Source of Truth 始终是 `project.db`。
3. **生成版本永不覆盖**——`GenerationVersion` 只增不改，冻结当次 prompt / workflow /
   context / 参数；没有任何 PUT/PATCH 能改写已存在的版本，只能
   `POST /versions/{id}/current` 换当前版本。
4. **绝不静默失败**——任何失败都是 `AppError(code, title, detail, suggestions)`；
   `app/main.py` 里的三个 handler 把请求校验错误和未捕获异常也归一成 `{"error": {...}}`，
   前端必须把 suggestions 显示出来。测试里 `tests/conftest.py::error_of` 会断言这一点。
   Tauri 侧的 `BootError` + `boot-error.html` 是同一契约的延伸（启动失败也绝不白屏）。

核心链路不可跳跃：`Character → Appearance → Scene → Shot → Context → Generation
→ GenerationVersion → Clip → Timeline → Final Video`。

## 当前进度（先读这段，别假设前端已经接上后端）

- **后端已全量落地**：docs/04 的 Step 1–9 都有实现——13 个 service（cast / world / assets /
  workflows / story / context / generation / timeline / overview / projects / library / adopt /
  fsbrowse）+ 15 个 router（含 `/ws`），152 个测试。
- **前端接上后端的是应用级两页**：项目管理 `features/project/ProjectsView.vue` +
  `stores/project.ts`、素材库 `features/library/LibraryView.vue` + `stores/library.ts`
  （`app/features.ts` 里 `projects` / `library` 是 `ready: true`）。
- **项目内 11 个功能页仍是外壳**：`/p/:pid` 下除概览外都渲染 `FeatureView`（按注册表画工作区
  骨架与能力锁），`ready: false`。做功能页是「把 FeatureView 换成实页面」，不是从零搭。
- 本机装了 `frontend/node_modules` 与 `backend/.venv`（后端命令用 `.venv/Scripts/python`），
  没有 Rust 工具链，`cd tauri && cargo tauri dev` 从未编译过。
- 本机装了 FFmpeg，所以 3 条断言「FFmpeg 缺失」的测试会失败，与改动无关：基线是
  `3 failed / 152 passed`。

## 常用命令

后端（Python 3.11+）：

```bash
cd backend && python -m pip install -e ".[dev]"
```

```bash
cd backend && python -m pytest -q
```

```bash
cd backend && python -m pytest tests/test_m4_generation_timeline_overview.py::test_cancel_and_retry_state_machine -q
```

```bash
cd backend && python -m ruff check . && python -m ruff format .
```

开发期起后端（固定端口，与 Vite 代理的默认目标一致；不传 `AIVS_PORT` 时是随机端口）：

```bash
cd backend && AIVS_PORT=8765 python -m app.main
```

前端：

```bash
cd frontend && npm install
```

```bash
cd frontend && npm run dev
```

```bash
cd frontend && npm run typecheck && npm run lint && npm run build
```

桌面壳（需要 Rust + `cargo install tauri-cli --version "^2"`；本机从未编译过）：

```bash
cd tauri && cargo tauri dev
```

迁移是**按工程库**跑的，没有全局数据库：

```bash
cd backend && python -m alembic -x db=<工程目录>/project.db upgrade head
```

```bash
cd backend && python -m alembic -x db=<工程目录>/project.db revision --autogenerate -m "描述"
```

配置全部走 `AIVS_` 前缀环境变量或 `backend/.env`（见 `backend/app/core/config.py`）：
`AIVS_PORT` / `AIVS_COMFY_BASE_URL` / `AIVS_FFMPEG_PATH` / `AIVS_WORKER_LIMIT` /
`AIVS_LLM_PROVIDER` / `AIVS_RUNTIME_DIR`。前端代理目标用 `AIVS_BACKEND` 覆盖。

## 架构要点

**进程与握手**：Tauri 壳（`tauri/src/backend.rs`）生成一次性 token 注入环境变量并拉起 Python
sidecar → 后端只监听 `127.0.0.1`，`port=0` 时由 OS 分配 → 把 `{host, port, base_url, ws_url,
token}` 写进 `.runtime/endpoint.json` → 壳校验后用 `window.__AIVS_ENDPOINT__` 注入前端。
开发期没有壳：前端走 Vite 代理打到固定端口 8765（`shared/api/endpoint.ts` 的同源回退），
`require_handshake` 默认关；开着时所有 `/api` 请求要带 `X-AIVS-Token`（WS 用 `?token=`）。

**后端分层**：`api/*.py` 只做 Pydantic body + 转调（刻意极薄）→ `services/*.py` 是业务，每个
模块导出一个单例（`generation = GenerationService()`）→ `persistence/` 是 ORM。公共件集中在
`services/base.py`：`db_of(pid)` / `fetch()` / `fetch_all()` / `as_dict()` / `load_json()` /
`dump_json()` / `assign()`——新代码用它们，不要每个模块重写一遍。

**每工程一个库，没有全局数据库**：`ProjectService`（`services/projects.py`）是全进程单例注册表，
维护 `pid → OpenProject{dir, db, ...}`。后端重启后进程内没有已打开的工程，此时任何
`/projects/{pid}/...` 都会 404「项目未打开」——这是设计而不是 bug，前端要引导回起始页重开。
`Database`（`persistence/db.py`）用一把 `asyncio.Lock` 串行化写、读走独立 session
（SQLite 单写者），并开 WAL + `foreign_keys=ON`。工程目录里已有无法识别的 `project.db` 时直接
报 `CONFLICT`，**绝不覆盖用户文件**。

**应用级素材库是唯一例外**（`services/library.py` + `persistence/models_library.py`）：用户自选一个
目录，里面是 `library.aivs.json` + `library.db` + `assets/`，跨工程复用素材文件与角色 / 地点 /
道具预设。它**不管理工程、不持有任何 Shot/Generation 数据**，工程仍是唯一真源。三条边界：

- 库表挂在自己的 `LibraryBase` 上，**绝不能** import 进 `persistence/all_models.py`——否则
  `alembic/env.py` 的 `target_metadata = Base.metadata` 会把库表 autogenerate 进工程迁移；
- 库不走 alembic：`open()` 时 `create_all`（幂等、只增表）+ 清单里的 `schema_version`，清单比当前
  应用新就 `SCHEMA_MISMATCH` 拒开。要改列时再加 `alembic_library/` 分支；
- 库位置记在 `settings.runtime_dir / "library.json"`（与 `recent.json` 同级的应用级状态），
  没配置时 `/library/*` 一律 `NOT_FOUND` + 「在素材库页选择一个目录」，不自动瞎建目录。

**采用是单向复制**（`services/adopt.py`，`POST /projects/{pid}/adopt`）：必须先 `adopt/plan` 出账单
（复制几个文件、多大、进哪个目录、哪些已经有了）再动手。文件复制进工程 `assets/`（沿用
`assets.register_path` 的 sha1 去重，重复采用不复制第二份），出处只是线索——`Asset.meta_json` 记
`{library_asset_id, library_sha1, adopted_at}`，Character / Location / Prop 上是 `origin_library_id`
列，**都不是外键，运行期不解析**。库关掉、目录改名，工程照常打开与列资产（`tests/test_adopt.py`
盯的就是这条）。之后库改了不回流工程，工程改了也不影响库，UI 必须把这句话写出来。

**文件服务与目录浏览**：`api/files.py` 的 `GET /projects/{pid}/files/{rel}` 与
`GET /library/files/{rel}` 是所有缩略图 / 预览的唯一来源（`<img src>` 带不了自定义头，所以只有它
们接受 `?token=`）；越界一律 `VALIDATION_ERROR`「路径越界」。`api/fs.py` + `services/fsbrowse.py`
提供 `/fs/roots`、`/fs/dirs`、`/fs/mkdir`——浏览器拿不到绝对路径，所以目录树必须由后端给，
`shared/ui/DirPicker.vue` 在浏览器与 Tauri 里走同一套；**只列目录，不返回任何文件内容**。

**事件**：进程内 `EventBus`（`events/bus.py`）→ 单个 `/ws` 端点按 `project_id` + `channels`
过滤（job / queue / shot / version / asset / system / error）。事件幂等、可丢失（队列满丢最旧），
前端重连后必须调 REST 做全量对齐；不要给它加投递保证或持久化。

**队列**：进程内调度，每个工程一个 pump task，`worker_limit` 控并发。`waiting` + `depends_on`
+ `wait_reason` 让「等上游镜头末帧」变成可解释的等待而不是卡住。

**Context Resolver**（`services/context.py`）：把「到底喂了什么给模型」变成一张账单——每条带
kind / priority / included / reason；人工覆写记在 `shot.context_overrides_json`（可 reset 回
自动），`snapshot()` 的结果冻结进 `GenerationVersion.context_json`。入队前
`require_complete()` 是硬门槛，`check_context=false` 才能显式跳过。

**时间线与导出**（`services/timeline.py`）：完全不依赖 AI。撤销栈是整轨快照（`UNDO_DEPTH=50`）；
`GET /export/command` 只产出 ffmpeg 参数计划，`POST /export` 才真的起进程。

## 代码约定

- **id**：`new_id("shot")` → `sht_<ULID>`。新实体必须先在 `app/core/ids.py` 的 `PREFIX` 里登记，
  否则直接抛 `ValueError`。
- **时间**：一律是 `utc_now()` 产出的 ISO 字符串（`String(40)` 列），不用 DateTime 类型。
- **JSON 列**：叫 `*_json` 的 Text 列存 JSON，读一律走 `load_json(raw, fallback)`
  （坏 JSON 退回默认值并保持可用，不抛）。对外输出时把 `*_json` 展开成干净字段再返回。
- **新增表**：工程表必须在 `persistence/all_models.py` 里 import，否则 `Base.metadata` 漏表；
  素材库表相反——挂 `LibraryBase`，**不要**进 `all_models.py`（理由见上面的素材库段）。
- **新增迁移**：`alembic/versions/` 加脚本 → 在 `persistence/migrate.py::REVISION_SCHEMA` 登记
  它对应的 schema 版本 → 同步 `settings.schema_version`（当前 3，最新一条是
  `0003_library_origin`：给 Character / Location / Prop 加 `origin_library_id`）。漏登记会导致
  打开旧工程时无法告诉用户「schema X → Y」。
- **落盘**：资产 `path` 相对工程目录存（整个目录拷走仍然有效）；类型→子目录映射在
  `services/assets.py::KIND_DIR`，`generations/` 只放生成物，手动素材一律进 `assets/`。
  所有落盘文件都要登记 `Asset` + `AssetRef`，删除前先说清会破坏什么。
- **外部依赖离线不是崩溃**：ComfyUI / FFmpeg / LLM 不可用时给带建议的结构化错误
  （`COMFY_OFFLINE` / `FFMPEG_MISSING` / `LLM_UNAVAILABLE`），并指出哪些路径不受影响。
- **前端加功能**：在 `app/features.ts` 登记一次（Activity Bar、入口页、命令面板、功能页四处共用
  这一份）。功能分 `scope`：`APP_DEFS` 是不用打开工程就有意义的（项目管理 / 素材库），
  `PROJECT_DEFS` 必须先打开工程——scope 由这两张表决定，不在条目里手写。route name 必须同名
  出现在 `app/router.ts`，进导航还要加进 `APP_NAV` / `PROJECT_NAV` 与 `NAV_LABEL`。
  REST 一律走 `shared/api/client.ts`（把 `{error}` 转成 `ApiError`，multipart 走 `api.upload`）；
  缩略图 URL 走 `shared/api/files.ts` 的 `fileUrl` / `libraryFileUrl`，不要自己拼路径。

## 测试约定

- 都是同步 `TestClient` 测试（`asyncio_mode=auto`）。autouse 的 `clean_runtime` 把
  `settings.runtime_dir` 指向 `tmp_path` 并在收尾时停掉所有 pump——工程是应用级状态，
  绝不能泄漏到下一个测试。
- 涉及入队的测试**先 `POST /queue/pause`**，pump 就不会真去连 ComfyUI；需要一个「已生成」的镜头时
  用 `POST /shots/{id}/versions` 手工造版本。
- conftest 提供 `error_of`（断言错误四要素齐全）、`ready_workflow`（导入 + `validate?probe=false`，
  本地绑定校验不需要 ComfyUI）、`upload_png`、`GRAPH` / `BINDINGS`；素材库侧是 `library`
  （在 `tmp_path` 下 configure 一个库，`clean_runtime` 收尾时 `library_service.shutdown()`）
  与 `lib_png`。
- **路径越界要用 `%2e%2e` 测**：httpx 会在发请求前折叠掉字面的 `..`，守卫根本轮不到执行。
  越界与其它 `VALIDATION_ERROR` 的状态码是 **422**（映射表在 `app/core/errors.py::_STATUS`）。
- 导出相关测试查 `GET /export/command` 的参数计划，不需要装 FFmpeg。

## 文档

`docs/01` 架构与选型 · `docs/02` 模块规格与 M0–M6 里程碑 · `docs/03` 全量表结构 + REST/WS 契约
+ 落盘规范 + 测试清单 · `docs/04` Step 1–9 与完成标准。service / api 的 docstring 里写的
「Step N」对应 `docs/04`；改接口或表结构时同步 `docs/03`。

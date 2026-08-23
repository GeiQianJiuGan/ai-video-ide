# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

AI Video Studio（`aivs`）：桌面端优先的 AI 原生长视频制作工作台。定位是
**AI = 素材生产器 · System = 视频工程与编排器 · Human = 导演**。
仓库内注释、错误文案、文档一律中文，新代码沿用这一习惯。

## 四条硬约束（改动前先确认没有违反）

1. **业务层不绑定具体视频模型**——不允许出现 `if model == "wan"` 或 `if provider == "comfy"`；
   差异全部下沉到 `app/generation/providers/*`（provider 适配层）。Shot 只写 capability
   （`text2image` / `image2video` / `first_last_frame` / `upscale`）与 provider 名。整个后端只有
   `providers/comfy_preset.py` 与 `app/generation/comfy/client.py` 知道 ComfyUI 存在；
   老的 Workflow 绑定表（`services/workflows.py::apply_bindings`）降级为兼容路径，
   只在 `job.workflow_id` 非空时才走。
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

- **后端已全量落地**：docs/04 的 Step 1–9 都有实现，之后又落了「两级场景系统」这一轮
  （应用级设置 → provider 适配层 → 衔接与编排 → 场景工作台 → 幕流程图 → AI 协作栏）——
  17 个 service（cast / world / assets / workflows / story / context / generation / timeline /
  overview / projects / library / adopt / fsbrowse / appsettings / frames / sequence / director）
  + 18 个 router（含 `/ws`），263 passed / 1 skipped。
- **前端也全部接上了后端**：`app/features.ts` 里 15 个功能都是 `ready: true`，
  `/p/:pid` 下不再有外壳页。`shared/ui/FeatureView.vue`（按注册表画工作区骨架与能力锁）
  暂时没人用，留给下一个「登记了但还没接后端」的功能——那种情况先挂它，绝不给假界面。
- 生成层的主路是 `flow`（幕）→ `scene`（场景工作台）；`workflows`（Workflow 管理）与
  `queue`（生成队列细看）都是 `advanced: true` 的兼容 / 细看路径，不在导航里，只从命令面板、
  设置页或底部控制台进——队列日常看的是控制台的任务框（见下面的「队列」段）。
- 单个能力仍会缺（ComfyUI 离线、没有 LLM）：这类按钮保持 `disabled` 并把原因写进 tooltip，
  不画假界面、不造假数据。
- 本机装了 `frontend/node_modules` 与 `backend/.venv`（后端命令用 `.venv/Scripts/python`），
  没有 Rust 工具链，`cd tauri && cargo tauri dev` 从未编译过；`tauri/src/backend.rs` 与
  `tauri.conf.json` 的改动无法在本机验证。
- **FFmpeg 随应用分发**，不再要求用户自己装（见下面的「内置 FFmpeg」段）。测试不依赖
  「这台机器上恰好装了/没装」：缺失路径用 `conftest.py::no_ffmpeg` fixture 造。

## 常用命令

一键起开发环境（`scripts/dev.py`：后端固定端口 + 前端 dev server + 依赖体检，起好自动开浏览器，
Ctrl+C 一起停；任一个子进程退出就把另一半也停掉，不留半死的环境）：

```bash
python scripts/dev.py
```

Windows 也可以直接双击仓库根的 `start.cmd`（macOS / Linux 是 `./start.sh`），它们只是找一个
Python 再转调上面那个脚本。常用参数：`--backend-only` / `--frontend-only` / `--port 8899` /
`--no-open`。下面两段是它内部实际跑的东西，需要单独调时照旧可用。

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

内置 FFmpeg（首次克隆仓库后跑一次；二进制约 150 MB，不进 git，`.gitignore` 忽略 `bin/ffmpeg*`）：

```bash
python scripts/fetch_ffmpeg.py
```

打包前把它们摆成 Tauri externalBin 要的 `<tool>-<target-triple>` 命名：

```bash
python scripts/fetch_ffmpeg.py --for-tauri
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
运行期还有一层**应用级设置**（配置页写的 `settings.json`）压在环境变量之上，
顺序是 **settings.json → 环境变量 → 默认**（见下面的生成层那段）。

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

**内置 FFmpeg**（`app/core/ffmpeg.py` 是唯一的查找入口，三个调用点
`services/timeline.py::_ffmpeg`、`api/system.py::_probe_ffmpeg`、`services/overview.py::environment`
都走它，**不要再写 `shutil.which("ffmpeg")`**）：抽帧 / 代理转码 / 导出都依赖它，它不该是一道
「先去装个东西」的门槛，所以应用自带一份。查找顺序 **显式配置 → 内置副本 → PATH**：

- 显式配置（`AIVS_FFMPEG_PATH` / `AIVS_FFPROBE_PATH` 写成含分隔符的路径）永远第一，指了却找不到时
  **不静默回退**到内置——那是配置写错了，`require()` 抛 `FFMPEG_MISSING` 说出来；
- 内置副本：`AIVS_BUNDLE_DIR`（Tauri 壳注入的主程序目录，externalBin 落点）→ 冻结后 sidecar 自身
  目录 → `<repo>/bin`（开发期 `scripts/fetch_ffmpeg.py` 的下载目标）；
- PATH 排最后：系统里那份版本未知，能用就用，但不该盖掉我们自带的。

`locate()` 返回 `Located{path, source, searched, configured_missing}`，`source` 会一路传到 UI
（概览页环境栏、状态栏、设置页）——「内置」和「你机器上那份」不是一回事，排查方向也不同。
刻意不做缓存：用户可能在应用开着的时候才去下载。打包时 `tauri.conf.json` 的 `externalBin` 列了
`bin/ffmpeg` / `bin/ffprobe`，构建前必须先 `python scripts/fetch_ffmpeg.py --for-tauri`，
否则 bundle 会失败。

**事件**：进程内 `EventBus`（`events/bus.py`）→ 单个 `/ws` 端点按 `project_id` + `channels`
过滤（job / queue / shot / version / asset / system / error）。事件幂等、可丢失（队列满丢最旧），
前端重连后必须调 REST 做全量对齐；不要给它加投递保证或持久化。连线上**只有一种信封**
（`{channel, event, project_id, ts, payload}`）：握手 `system.connected` 与心跳 `system.ping`
也必须过 `Event(...).to_dict()`，不许在 `api/ws.py` 里手写字面量——少一个 `ts`，前端按契约
读它的那一处就白屏（`tests/test_m0_foundation.py::test_ws_connect_and_receive_event` 盯着）。

**队列**：进程内调度，每个工程一个 pump task，`worker_limit` 控并发。`waiting` + `depends_on`
+ `wait_reason` 让「等上游镜头末帧」变成可解释的等待而不是卡住。前端侧它**不是一个页面而是
底部控制台**（`app/layout/ConsolePanel.vue`：任务框 + 日志框，入口是状态条上那个任务标识，
Ctrl + \` 开合，高度记在 localStorage）。控制台常驻，所以 **WS 订阅归它**
（`queue.connect` / `disconnect` / 切工程时 `reset()`），队列页只 `load()`——以前订阅挂在队列页上，
一离开页面实时通道就断了。`features/generation/QueueView.vue` 还在，但是 `advanced: true`、
不进 `PROJECT_NAV`，只从命令面板或控制台的「队列页」按钮进，看失败现场与冻结参数。

**Context Resolver**（`services/context.py`）：把「到底喂了什么给模型」变成一张账单——每条带
kind / priority / included / reason；人工覆写记在 `shot.context_overrides_json`（可 reset 回
自动），`snapshot()` 的结果冻结进 `GenerationVersion.context_json`。入队前
`require_complete()` 是硬门槛，`check_context=false` 才能显式跳过。采用的条目还带一个
`role`（`first_frame` / `reference`）——**「哪一张当首帧」这条规则只在 `_assign_roles` 这一处**，
`services/generation.py::_images_of` 照账单读它，绝不在生成层再挑一遍（两边各挑一次的话，
检查器上标的和真正喂进去的会分叉）。上限是应用级设置 `video.ref_limit`（默认 8，`ref_limit()`）。

**时间线与导出**（`services/timeline.py`）：完全不依赖 AI。撤销栈是整轨快照（`UNDO_DEPTH=50`）；
`GET /export/command` 只产出 ffmpeg 参数计划，`POST /export` 才真的起进程。

**生成层 = 两级场景系统 + provider 适配层**（不再是「Workflow 为中心」）：

- **第一级：幕流程图**（`services/sequence.py` + `api/sequence.py`，前端 `features/flow/FlowView.vue`）。
  一个节点是一幕（`Scene`），节点之间那一条是**衔接**（新表 `SceneLink`，三种 mode）：
  `cut` 不生成任何东西；`transition` 补一段 1~2s 转场视频，落成一个 `Shot.kind="transition"`
  的镜头——**属于 from_scene 且排在它最后**，于是 `timeline.auto_assemble` 的「scene.index_no +
  shot.index_no」排序天然把它放在两幕之间，导出逻辑一行不用改；`tail_frame` 只是把下游首镜头的
  `prev_shot_id` 指到上游末镜头，复用已有的 `depends_on` / `wait_reason`。
  编排两种模式（`parallel` / `sequential`）**一律先账单再动手**：`POST /sequence/plan` 只读地
  列出「入队几个任务、补几段转场、哪一条缺什么」，`POST /sequence/run` 才真入队；被跳过的每条
  都带四要素错误，跳过不是失败。
- **镜头之间也有那条线**（`ShotLink`，迁移 `0012_shot_link`；前端画在
  `features/story/StoryboardView.vue` 的卡片之间）：能引用设定图的模型往往做不了严格首尾帧，
  反过来能严格首尾帧的又引用不了设定图，所以「两镜之间补一段短转场」是这类模型下唯一能把画面
  接上的路。与 `SceneLink` 同一套形状，但**刻意没有 `tail_frame`**——镜头级的「续接末帧」早就有
  表达方式了（`Shot.prev_shot_id`），再给它一个同义词只会让两处配置打架。四条规矩：
  **没有行就是无转场**（老工程一行不用建，画线也不需要先落库）；两头必须**同幕且相邻**，
  跨幕请用幕级那条；补出来的转场镜头**排在这两镜之间**，所以导出与时间线装配一行不用改；
  **「转场暂未生成」只认 `GET /storyboard` 连接器上的 `pending`**
  （`mode == 'transition' and not generated`，`generated` 说的是那个转场镜头有了成片）——
  界面绝不拿 `transition_shot_id` 再算一遍，否则一入队就会谎报「已生成」。
  一键生成转场是 `POST /sequence/transitions/plan` → `.../run`（`only` 收衔接 id，两级共用，
  已有成片的一条都不重做）。**转场只在分镜那一页配**：时间线的片段属性里没有转场，
  后端的 `Transition` 接口原样留着当兼容路径，但不再有界面入口——两处都能配的话必然打架。
- **节点里的小节点**（`services/story.py` + `persistence` 里 `SceneCast` / `SceneLocation`，前端
  `features/flow/SceneNodeCard.vue` + `SceneNodeInspector.vue`）：一幕是一张小图表，挂着三种小
  节点——**prompt 必填**（`Scene.prompt`，缺了 `graph()` 会把它写进 `issues`），**人物 / 地点可以
  一个都不选**但各自不超过 `story.node_limit()`（应用级设置 `scene.node_limit`，默认 9，运行期
  可配，所以前端只显示 `N/上限` 并提前禁用，真正的守卫在后端）。地点表的**第一条同时是主地点**，
  同步 `scene.location_variant_id`——「设为主地点」= 挪到第一位，不是另一个字段。超上限的四要素
  错误里那句「上限可改：设置页…」只有一处口径：`story.py::LIMIT_HINT`。小节点必须真的影响生成，
  不是装饰：镜头没挂自己的出场表时 `context.resolve()` 就继承这一份。
  **挑的时候看图不看 id**：`GET /projects/{pid}/scene-node-options` 一次给两张清单
  （`cast[]` / `locations[]`，各带 `label` 与相对工程目录的 `thumbnail_path`，没图给 `null` 但
  仍留在清单里）+ `node_limit` / `limit_hint`；`GET /scenes/{sid}` 回的 `cast[]` / `locations[]`
  也带同一个 `thumbnail_path`。前端一律走 `shared/ui/AppThumb.vue`（缺图退化成占位块，
  绝不显示碎图标），**不要**再按角色 / 变体一个个拉 `appearances` / `references` 拼 N+1。
- **「用哪一段」只挂在镜头上，幕上没有第二个指针**：一幕下面很多镜头，每个镜头各自独立生成
  很多段，所以这件事只能一个镜头一个镜头地定——就是 `Shot.current_version_id`，
  `timeline.auto_assemble` 装配的、下游镜头抽末帧认的都是它，采用只走全工程唯一那个入口
  `POST /projects/{pid}/versions/{version_id}/current`（硬约束 3；新版本入库时自动成为当前版本，
  所以刚生成完就是「已采用」，换一段就是再采用一次，旧版本一条都不删）。
  `GET /scenes/{sid}/videos` **按镜头分组**列候选（`shots[]`，每组带 `adopted_version_id` 与
  `items` / `omitted`，不能当候选的进 `omitted` 并附原因），前端照组渲染，不再有幕级别的
  「主视频」。历史上幕上另存过一个 `Scene.main_version_id`：镜头一搬到别的幕那个指针就发霉，
  只能靠 `issues` 报「已失效」——`0006_shot_adopted_video` 把它回填进
  `shot.current_version_id` 后删掉了这一列。
- **节点上播的那一段**：`graph()` 的节点带 `video_path`（能播的 `<video>`）与 `thumbnail_path`
  （只会是图片），**两个字段绝不混用**。挑用哪一段的顺序是「按镜头顺序 → 同一镜头内采用的
  那一版优先 → 否则最新的那一版」，并带上 `video_shot_id`（播的这段属于哪个镜头）与
  `video_adopted`；整幕都没采用过视频时也不显示「暂无」——已经出片了却看不见比挑错一段更糟，
  此时 `video_adopted=false`，界面上标出「播的只是自动挑的一段」，不假装是。
  **这条「视频 / 缩略图分开给」的规矩三处共用**：幕节点（`sequence.graph()`）、分镜板卡片
  （`story._shot_media`）、版本轨（`generation._version_media`）。缩略图只有两个来源——那一版本身
  就是图片，或这段视频**已经抽过首帧**（`frames.start_frame_index`，`Asset(kind="frame")` 的
  `meta_json.at == "start"`，解读只放在这一个函数里）；一张都没有时只回 `video_path`，
  `<video preload="metadata">` 自己会画第一帧，**读路径绝不顺手起 FFmpeg**（补抽走分镜板的
  `POST /storyboard/posters` 这个显式入口）。把 `.mp4` 塞进 `<img>` 只会得到一个坏图标——
  「分镜里截取的首帧加载失败」与版本轨上那一排坏图都是这么来的。
- **第二级：场景工作台**（前端 `features/flow/SceneWorkbench.vue`）：单幕的首帧 / 末帧槽位 →
  R2V 生成 → 版本轨。**本轮没有 T2V。** 从第一级过去的手势是**双击节点**（单击只选中）。
- **AI 协作栏**（`app/ai/director/` + `services/director.py` + `api/director.py`，前端
  `features/flow/DirectorPanel.vue`）：`ai/director/tools.py` 里那条**读 / 写分界就是安全边界**——
  读工具（`list_*` / `get_scene`）立刻执行，写工具（`add_scene` / `set_link` / …）**永不落库**，
  只翻译成一条提案 `{op, target, temp_id, before, after, why, warnings}`。`chat` 一行库都不改，
  只有 `POST /director/apply` 才落，且只落 `op != "reject"` 的条目（照
  `story.propose_breakdown` / `apply_breakdown` 的老规矩），逐条转调已有的 `story` / `sequence`
  写方法——绝不另写一份写库逻辑。工具循环上限 `agent.MAX_ROUNDS = 6`；转满轮数时**先把提案落成
  `DirectorTurn` 记录再报错**，已产出的提案照旧可审阅。不支持 function calling 的端（Ollama）
  退化成一次性 `complete_json()`，提案形状完全一样。会话与提案存 `DirectorTurn`（只增不改），
  审阅到一半刷新页面不丢。
- **provider 适配层**（`app/generation/providers/`）：`base.py` 定义与模型无关的 `VideoRequest`
  （`mode` = `i2v` / `flf`、prompt、首尾帧、**参考图 `refs`**、时长、seed、透传 `extra`、
  降级说明 `notes`）与 `VideoProvider` 协议（`probe` / `submit` / `poll` / `fetch`）；
  `comfy_preset.py` 是默认核心，`http_api.py` 是通用 REST 合同，`registry.py::provider()`
  按应用级设置选。
  **本工具不维护模型端的图**：ComfyUI 适配器只按**节点 title 约定**注入入口参数——
  `AIVS_FIRST_FRAME` / `AIVS_LAST_FRAME` / `AIVS_PROMPT` / `AIVS_NEGATIVE` / `AIVS_DURATION` /
  `AIVS_SEED` / `AIVS_REF_1`…`AIVS_REF_9`——不解析、不校验、不改写图里的 lora 与加速节点。
  缺必需 title 时报 `INVALID_WORKFLOW`，建议里写「在 ComfyUI 里把该节点标题改成 X」。
  lora、加速节点、采样器怎么摆是模型端自己的事，本工具跟着改迟早两边打架。
- **首尾帧 ≠ 参考图**（`AIVS_REF_*` 那一组就是为这件事加的）：首尾帧决定「画面从哪一格开始 /
  结束」，参考图决定「谁出场、在哪儿」。只喂一张首帧最容易丢的就是人物形象，所以账单里采用的
  条目**除首帧那一张之外全部当参考图送到模型端**（`generation._images_of`）。
  **槽位不够只降级、不失败**：图里标了 3 个而账单给了 5 张就填前 3 张，把少喂了哪几张写进
  `req.notes` → 冻结成版本参数 `ref_notes`（`refs` 记实际喂了哪几张），界面上看得见。
  一个 `AIVS_REF_*` 都没有的预设照样 `ready`，只是设置页的预设列表会把「参考图 0 槽」
  标成警告。默认会在 prompt 末尾附一句 `参考图说明：参考图1=…`（`base.ref_hint`，
  ComfyUI 那类图收不到标签，只能靠这句对号），设置里 `video.ref_labels` 可关。
- **真末帧抽取**（`services/frames.py`）：`tail_frame` 衔接靠它。FFmpeg `-sseof` 抽一张 PNG →
  登记 `Asset(kind="frame")` → 同 (asset, at) 幂等复用；`services/context.py` 的 `prev_frame`
  指的就是这张抽出来的帧，不是上游那整段视频。抽取失败报 `FFMPEG_ERROR`，建议里给出
  「改用转场衔接」这条出路。
  **抽出来的首尾帧是临时资源，不是工程资产**：它落 `cache/frames/`（`KIND_DIR["frame"]`）而不是
  `assets/`，在 `assets.TRANSIENT_KINDS` 里，所以**不进资产总账、不算孤儿**（要看它们得显式
  `?kind=frame`）——它没有任何 `AssetRef`，算进孤儿列表只会把「可以回收的文件」这份清单刷满。
  它仍然是一行 `Asset`：上下文账单与 `_images_of` 都是靠 `asset_id → path` 取文件的，从登记里
  拿掉就得另造一套解析。**源成片一删，从它抽的帧跟着删**（`assets.delete` 走
  `frames.derived_frames` 认 `meta_json.from_asset_id`），删了哪几张回在 `derived_removed` 里，
  界面必须说出来——连带删除绝不静默。反过来不成立：单独删一张帧不动成片，需要时重抽一次就有。
- **应用级设置**（`services/appsettings.py` + `api/settings.py`）：落
  `settings.runtime_dir / "settings.json"`（与 `library.json` / `recent.json` 同级），生效顺序
  **settings.json → 环境变量 → 默认**，每个字段回一个 `source` 让 UI 标出「来自配置文件 /
  环境变量」。`POST /settings/probe` 分别探 LLM 与视频服务。**API key 永不回明文**：只回
  `masked` + `has_value`，前端只在用户真的输入了才提交那个字段。
- **LLM 协议适配层**（`app/ai/llm/protocols.py`，`client.py` 只按设置选一个协议再转调）：与
  provider 适配层同一个思路——**业务层只有 OpenAI 那套内部规范形状**（`messages` / `tools` /
  `tool_calls`），四种方言（`openai_compatible` / `anthropic` / `gemini` / `ollama`）的差异全部
  下沉到这一个文件里。三条不许绕的规矩：
  - **协议表是唯一真源**：默认地址 / 要不要密钥 / 支不支持工具 / 模型列表从哪来都写在
    `BY_NAME` 里，`GET /settings` 把它投影成 `llm_protocols[]` 给前端画界面——加一个协议
    只改这一个 dict，前端一行不动；
  - **密钥只走请求头**（`authorization` / `x-api-key` / `x-goog-api-key`），**任何协议都不把它
    放进 URL**——进了 URL 就会跟着日志和四要素错误一起漏出去（Gemini 刻意不用 `?key=`）；
  - **不支持工具不等于用不了**：`supports_tools=False` 的端（Ollama）退化成一次性
    `complete_json()`，提案形状完全一样，错误建议里必须写明这一点。
  「自动获取模型」是 `POST /settings/models`：带上**还没保存**的协议 / 地址 / 密钥先列一遍，
  **绝不落盘**（不然得先存一份可能是错的配置）；`current_present=false` 表示连得上但这个端上
  没有当前模型。所有出网请求走 `protocols._client(timeout)` 这唯一出口——
  `tests/test_llm_protocols.py` 就是靠 monkeypatch 它把 24 个用例全部关在机器里跑。
- **老的「Workflow 管理」是高级 / 兼容路径**：页面与代码原样保留（`features.ts` 里
  `advanced: true`、不进 `PROJECT_NAV`），`GenerationService._execute` 默认走 provider，
  只有 `job.workflow_id` 非空时才走 `apply_bindings` 那一支。

## 代码约定

- **id**：`new_id("shot")` → `sht_<ULID>`。新实体必须先在 `app/core/ids.py` 的 `PREFIX` 里登记，
  否则直接抛 `ValueError`。
- **时间**：一律是 `utc_now()` 产出的 ISO 字符串（`String(40)` 列），不用 DateTime 类型。
- **JSON 列**：叫 `*_json` 的 Text 列存 JSON，读一律走 `load_json(raw, fallback)`
  （坏 JSON 退回默认值并保持可用，不抛）。对外输出时把 `*_json` 展开成干净字段再返回。
- **新增表**：工程表必须在 `persistence/all_models.py` 里 import，否则 `Base.metadata` 漏表；
  素材库表相反——挂 `LibraryBase`，**不要**进 `all_models.py`（理由见上面的素材库段）。
- **新增迁移**：`alembic/versions/` 加脚本 → 在 `persistence/migrate.py::REVISION_SCHEMA` 登记
  它对应的 schema 版本 → 同步 `settings.schema_version`（当前 12，最新一条是
  `0012_shot_link`：镜头之间那条衔接，与 `scene_link` 同一套形状但**没有 `tail_frame`**，
  没有行就是无转场）。漏登记会导致
  打开旧工程时无法告诉用户「schema X → Y」。
- **落盘**：资产 `path` 相对工程目录存（整个目录拷走仍然有效）；类型→子目录映射在
  `services/assets.py::KIND_DIR`，`generations/` 只放生成物，手动素材一律进 `assets/`，
  可再生的临时文件（抽出来的首尾帧）进 `cache/`。
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
  `settings.runtime_dir` 指向 `tmp_path`、重新 `app_settings.apply()` + `provider_registry.reset()`，
  并在收尾时停掉所有 pump——工程、素材库、应用级设置、provider 实例都是应用级状态，
  绝不能泄漏到下一个测试。
- 涉及入队的测试**先 `POST /queue/pause`**，pump 就不会真去连 ComfyUI；需要一个「已生成」的镜头时
  用 `POST /shots/{id}/versions` 手工造版本。
- conftest 提供 `error_of`（断言错误四要素齐全）、`ready_workflow`（导入 + `validate?probe=false`，
  本地绑定校验不需要 ComfyUI）、`upload_png`、`GRAPH` / `BINDINGS`；素材库侧是 `library`
  （在 `tmp_path` 下 configure 一个库，`clean_runtime` 收尾时 `library_service.shutdown()`）
  与 `lib_png`。
- **LLM 一律 monkeypatch 掉**（`tests/test_director_agent.py::use_fake_llm` 是范本：改
  `settings.llm_provider` / `llm_model` / `llm_base_url` 再换掉 `llm.complete_tools`）——
  测的是提案不落库、apply 只落未 reject 的这些边界，不是某个模型的脾气。
- **路径越界要用 `%2e%2e` 测**：httpx 会在发请求前折叠掉字面的 `..`，守卫根本轮不到执行。
  越界与其它 `VALIDATION_ERROR` 的状态码是 **422**（映射表在 `app/core/errors.py::_STATUS`）。
- 导出相关测试查 `GET /export/command` 的参数计划（只有 `{path, command, clips}`，没有 `args`），
  不真起进程。
- **「缺 FFmpeg 时怎么报错」要用 `no_ffmpeg` fixture 造**，别靠「这台机器上恰好没装」——应用现在
  自带一份，那样的断言在开发机与 CI 上结论相反。反过来，「用的是内置那份」的正向测试在没跑过
  `fetch_ffmpeg.py` 的机器上 `pytest.skip`。

## 文档

`docs/01` 架构与选型 · `docs/02` 模块规格与 M0–M6 里程碑 · `docs/03` 全量表结构 + REST/WS 契约
+ 落盘规范 + 测试清单 · `docs/04` Step 1–9 与完成标准 · `docs/05` 三种调用方式与模型端要
准备什么（`AIVS_*` 节点标题约定、http_api 合同、最小验收清单）。service / api 的 docstring
里写的「Step N」对应 `docs/04`；改接口或表结构时同步 `docs/03`，改生成层的入口约定或适配器
时同步 `docs/05`。

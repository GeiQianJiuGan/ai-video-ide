# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

AI Video Studio（`aivs`）：桌面端优先的 AI 原生长视频制作工作台。定位是
**AI = 素材生产器 · System = 视频工程与编排器 · Human = 导演**。
仓库内注释、错误文案、文档一律中文，新代码沿用这一习惯。

## 四条硬约束（改动前先确认没有违反）

1. **业务层不绑定具体视频模型**——不允许出现 `if model == "wan"` 或 `if provider == "comfy"`；
   差异全部下沉到 `app/generation/providers/*`（provider 适配层）。Shot 只写 capability
   （`text2image` / `image2video` / `first_last_frame` / `upscale`）与 provider 名。知道 ComfyUI
   存在的只有 `providers/comfy_*.py`（共用 `comfy_base.py`）与 `app/generation/comfy/*`
   （外加两处兼容 / 只读用途：`services/workflows.py` 导入与校验那份图、`services/overview.py`
   探测服务在不在）。**全后端按调用方式的名字分岔只有一处**：`services/route.py::BINDS`
   （「这条路要绑什么」那张表，界面照 `binds` 画控件）——service 层与前端一个名字都不写死。
   **走哪条路是入队时解析一次并冻结进 `job.params.route`，执行与重试只读冻结值**
   （见下面生成层那段的「工程路由」）。
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
  （应用级设置 → provider 适配层 → 衔接与编排 → 场景工作台 → 幕流程图 → AI 协作栏）与
  「工程路由」这一轮（`services/route.py` 一份口径 + `comfy_workflow` 提成一等适配器）——
  26 个 service（cast / world / assets / workflows / story / context / generation / timeline /
  overview / projects / library / adopt / fsbrowse / appsettings / frames / sequence / director /
  packages / ingest / dub / refine / audio / images / describe / onboarding / **route**；`base` /
  `params` / `global_registry` 是公共件不算）+ 25 个 router（含 `/ws`），480 passed（1 skipped：
  没跑过 `fetch_ffmpeg.py` 的机器上跳过「用的是内置那份」那条正向断言）。
- **前端也全部接上了后端**：`app/features.ts` 里 15 个功能都是 `ready: true`，
  `/p/:pid` 下不再有外壳页。`shared/ui/FeatureView.vue`（按注册表画工作区骨架与能力锁）
  暂时没人用，留给下一个「登记了但还没接后端」的功能——那种情况先挂它，绝不给假界面。
- 生成层的主路是 `flow`（幕）→ `scene`（场景工作台）；`workflows`（Workflow 管理）与
  `queue`（生成队列细看）都是 `advanced: true` 的高级 / 细看路径，不在导航里，只从命令面板、
  设置页或底部控制台进——队列日常看的是控制台的任务框（见下面的「队列」段），
  Workflow 管理是 `comfy_workflow` 那条路绑图的地方（见下面的「工程路由」段）。
- 单个能力仍会缺（ComfyUI 离线、没有 LLM）：这类按钮保持 `disabled` 并把原因写进 tooltip，
  不画假界面、不造假数据。
- 本机装了 `frontend/node_modules` 与 `backend/.venv`（后端命令用 `.venv/Scripts/python`），
  没有 Rust 工具链，`cd tauri && cargo tauri dev` 从未编译过；`tauri/src/backend.rs` 与
  `tauri.conf.json` 的改动无法在本机验证。
- **打包流水线已经接上了**（`scripts/build_desktop.py`，见下面的「打包与分发」段与
  docs/06）：图标与 PyInstaller sidecar 不再是缺口，sidecar 在本机打过、启动过、建过工程；
  只有最后一步 `cargo tauri build` 因为没有 Rust 工具链没跑过。
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

出安装包（一条命令跑完图标 → FFmpeg → sidecar → `cargo tauri build`，详见 docs/06）：

```bash
python scripts/build_desktop.py
```

只体检、缺什么说什么（不构建）：

```bash
python scripts/build_desktop.py --check
```

单独打后端 sidecar（PyInstaller onefile，打完会真的启动一次并建一个空工程自检）：

```bash
python scripts/build_sidecar.py
```

出 Docker 镜像 tar（体检 → 版本对账 → build → 启动自检 → `docker save`，落 `dist/docker`；
`--target split` 出 compose 那两个镜像，`--check` 只体检，详见 `docs/docker-deployment.md`）：

```bash
python scripts/package-docker.py
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

**跨环境搬迁 = 工程包 / 场景包**（`services/packages.py` + `api/packages.py`，前端
`features/packages/`）：一个包是一个 zip（`.aivspkg`，`manifest.kind = "aivs-package"`），
两种粒度——整个工程（`project.db` 的 `backup()` 快照 + `assets/`，勾了才带 `generations/`）
与单独一幕（行级快照，能导进**任意**已打开的工程）。它照 `services/adopt.py` 的规矩办：
**先账单再动手**，落库全部转调已有写方法，不新增写路径、不加迁移、不加列
（出处只进 `Asset.meta_json`，`ids.PREFIX` 只多一项 `"package": "pkg"`）。六条不许绕的：

- **落点与来源的主路是用户那台机器，不是后端机器上的一个路径**：界面跑在浏览器 / WebView 里，
  拿不到也不该猜那台机器上的路径。导出走 `GET …/package/download`（包写进后端临时目录 →
  当附件流回来 → 流完 `BackgroundTask` 删掉临时目录），前端必须 **fetch 回 Blob 再
  `saveBlob`**——握手开着时 `<a href>` 带不了 `X-AIVS-Token`（`?token=` 只在路径含 `/files/`
  的 GET 上接受），文件名以 `Content-Disposition` 为准（非 ASCII 只有 RFC 5987 那一支）。
  导入走 `POST /packages/upload`（分片落进 `<runtime_dir>/uploads/<pkg_id>/`），回的形状
  **和 `inspect` 完全一样**、只多 `staged` / `name`，所以两个导入入口一行都不用改。
  暂存副本**三层收拾**：导入成功后服务层删、用户取消 / 关窗 / 换包时前端调
  `/packages/staged/discard`（**只认暂存区里的路径**，指到别处一律拒绝——磁盘上那份包是用户
  自己的东西），都漏了还有每次上传前按 TTL 扫一遍。「写进后端机器上某个目录 / 读那台机器上
  某个路径」那条老路**降级成第二条、但不许删**：桌面版里两台机器就是同一台，几个 G 的包不必
  自己传给自己一遍。**导出的入口只在工程里**（标题栏 / 命令面板 / 概览页 / 幕检查器），
  项目列表页只有导入——那一页上根本没有打开的工程。
- **包里不带预设图，只带一份环境要求清单**（`manifest.env`）。图属于用户那台 ComfyUI，
  本工具从来不维护它（见上面的 provider 适配层那段），带走一份只会在目标机器上和那边真正
  装了什么打架。清单里 `presets[].markers` 是从导出机上那份预设数出来的**入口标题**，
  于是目标机器没有同名预设时，至少能说出「要一份标了这几个入口的图」。
- **密钥与服务地址一律不进包**：`settings.json` 不是包成员，manifest 里也没有任何
  `api_key` / `base_url`。理由和「API key 永不回明文」是同一条——包会被随手转发。
- **`cache/` / `proxies/` / `.runtime/` 永不进包**（可再生），成片默认不带、可勾选。
- **导入时必须重新生成 project id**（`new_id("project")`，同时改 `project` 表那一行）：
  `ProjectService._open` 是按 pid 索引的，同机导入一份副本后两个目录同 id 会互相顶掉。
  别「顺手」把它改回保留原 id。
- **`inspect` 与导入那道门必须读同一个数**：schema 比对读的是顶层
  `manifest["schema_version"]`（不是 `env.schema_version`），否则 inspect 说「吃得下」、
  到导入才 `SCHEMA_MISMATCH`。包内成员越界（绝对路径 / 盘符 / `..` / symlink）在写第一个字节
  之前全部查完，报 `VALIDATION_ERROR`「包内路径越界」。

`plan` 的 `omitted[]`（跨幕 `SceneLink`、幕外 `prev_shot_id`、job 历史、时间线、`DirectorTurn`）
前端**原样显示**——跳过不是失败，但必须说出来。

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

**打包与分发**（`scripts/build_desktop.py` 是唯一入口，详见 docs/06）：目标是**装完就能用**
——Python 解释器、后端依赖、FFmpeg 全在包里。五步顺序不能换：图标（`scripts/make_icons.py`）
→ 内置 FFmpeg（`fetch_ffmpeg.py --for-tauri`）→ sidecar（`build_sidecar.py`）→ 前端
（`beforeBuildCommand`）→ `cargo tauri build`；`cargo tauri build` 会在编译完十分钟之后才因为
「少一张图标」失败，所以体检与前置产物都在脚本里前置。**没法交叉编译**：Windows 包只能在
Windows 上出、Linux 包只能在 Linux 上出，两个平台的产物靠 `.github/workflows/release.yml`
的双机 matrix。四条不许绕的：

- **冻结后有两套路径算法，方向相反**，`tests/test_packaging_paths.py` 两边都盯着：
  `config._repo_root()` → **可执行文件所在目录**（externalBin 把 ffmpeg 装在那一层）；
  `migrate._backend_root()` → **解包目录 `sys._MEIPASS`**（迁移脚本作为数据文件摆在那儿）。
  照 `__file__` 往上数几级会指到系统临时目录，而且每次启动都变。
- **alembic 是数据不是代码**：迁移脚本是运行期 exec 的 `.py`，静态分析看不见，必须显式进
  spec 的 `datas`，落点与 `_backend_root()` 对得上。漏了这条后端能启动、`/health` 正常，
  **一建工程就炸**——所以 `build_sidecar.py` 打完会真的启动一次并建一个空工程。
- **sidecar 必须是 `console=True`**：windowed 构建里 PyInstaller 会掐掉 `sys.stderr`，
  Python 堆栈就进不了 `runtime/backend.stderr.log`，壳只剩一个退出码可报——那是静默失败
  （硬约束 4）。黑框由壳的 `CREATE_NO_WINDOW` 与 spec 的 `hide_console` 两处按住。
- **onefile 一个文件是两个进程**（bootloader + 真正的 Python），退出时必须**按树杀**
  （`Supervisor::shutdown` 与 `build_sidecar._kill_tree` 各一份）。只杀外层的话里层变孤儿，
  继续占着 `project.db` 与那个回环端口，任务管理器里却看不到我们的程序。
  同一个原因 `START_TIMEOUT` 是 90s：解包几十 MB 比源码树那条路慢得多。

图标**提交进版本库**（纯 Python 画的，打包机不必装 Pillow 或跑 `npx`）；有设计稿就放
`tauri/icons/source.png` 再 `make_icons.py --force`。

**Docker 分发是另一条路**（`scripts/package-docker.py` 是唯一入口，详见
`docs/docker-deployment.md`）：一条命令走完「体检 → 版本对账 → build → 启动自检 →
`docker save`」，落 `dist/docker/<镜像>-<版本>-<平台>.tar` + 一份同名 `.json` 回执
（tag / commit / 镜像 id / 自检结论 / 搬过去怎么跑）。五条不许绕的：

- **镜像名只有那张 `Image` 表**（`aivs-allinone` / `aivs-backend` / `aivs-frontend`），
  `docker-compose.yml` 的 `image:` 与它同源。compose 里不写 `image:` 就会按目录名瞎起
  （`xunjie_video_ide-backend` 就是那么来的），于是「这个 tar 里装的是什么」每次都不一样。
- **版本号只有一个源头** `tauri/tauri.conf.json`，另外三处（`frontend/package.json` /
  `backend/pyproject.toml` / `backend/app/core/config.py`）对不上就报错 + 给 `--sync-version`，
  **绝不替用户挑一个**。同一个数同时进 tag、OCI 标签与容器里的 `AIVS_VERSION`
  （两份 Dockerfile 的 `ARG AIVS_VERSION` → `ENV`；`AIVS_` 前缀会盖掉 `Settings.version`，
  所以 `/health` 报的与 tar 文件名永远是同一个数）。手敲 `docker build` 不传这个 build-arg，
  打出来的镜像自报 `0.0.0-dev`——那正是「版本名不一致」的来源。
- **导出前每个镜像真跑一次**（照 `build_sidecar.py` 的规矩：产物必须自己启动过才算打完）：
  探静态首页与 `/api/v1/health`、核对自报版本号，跑不起来就不写 tar。**502 / 503 要接着等**
  ——all-in-one 里 nginx 先起、uvicorn 后起，那几秒的 502 是启动过程不是启动结果，
  只有「容器已经退了」才提前结束等待。`aivs-frontend` 刻意不自检并把原因写进回执
  （它的 nginx 启动时就要解析 `backend:8765` 这个上游，单独跑必然 emerg 退出）。
- **进容器执行的东西必须是 LF**：根目录 `.gitattributes` 把 `*.sh` 钉成 `eol=lf`，
  `Dockerfile` 里另有一道 `sed -i 's/\r$//'` 兜底。Windows 上 `core.autocrlf` 检出的 CRLF
  shebang 会让内核去找 `/bin/bash\r`，容器只报一句
  `exec /app/entrypoint.sh: no such file or directory`（文件其实就在那儿）。
- **平台只进文件名不进构建参数**：**不替用户交叉编译**（那要 buildx + QEMU，失败信息又长又难认），
  但 amd64 的 tar 在 arm64 上 load 出来是个跑不起来的容器，所以架构必须写在名字上。

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
kind / priority / included / reason / **media**（`image` / `video` / `audio`，只看后缀）；
人工覆写记在 `shot.context_overrides_json`（可 reset 回自动），`snapshot()` 的结果冻结进
`GenerationVersion.context_json`。入队前 `require_complete()` 是硬门槛，`check_context=false`
才能显式跳过。采用的条目还带一个 `role`（`first_frame` / `last_frame` / `reference`）——
**「哪一张是首帧」只认显式槽位**（`Shot.first_frame_asset_id` / `last_frame_asset_id`，
用户按下去的那一下），首帧没指定而这个镜头要续接上游时才用 `prev_frame` 那张真末帧，
**到此为止，绝不提拔参考素材**。以前这里把优先级最高的那一条（通常是角色三视图）自动标成
首帧，于是画面从一张三视图开始——那是「默认首张就是首帧」那个 bug 的根源。判定规则只有一份
（`_assign_roles`），`services/generation.py::_images_of` 照账单读它，绝不在生成层再挑一遍
（两边各挑一次的话，检查器上标的和真正喂进去的会分叉）。**账单不截断**：采用的照样全采用，
超出槽位的部分变成生成前的一次确认（`REF_OVER_CAPACITY` + `allow_ref_drop`），
真正的截断只发生在提交那一刻并如实写进 `params.ref_notes` / `params.refs`。上限来自**这个工程
这个能力真正会提交的那条路**（`route.capacity()` 一份口径，`context.project_ref_capacity` 只把
`capability` 传下去）：预设路数那份图上的 `AIVS_REF_*`、REST 路不限量（`None`）、绑定路只收图片
（视频 / 音频是实打实的 `0`）。**既不是应用级设置**（`video.ref_limit` 已经不再是上限来源），
**也不能只看一份预设**——首尾帧镜头提交的是 `flf` 那份图，照 R2V 那份数出来的数字是假的。

**时间线与导出**（`services/timeline.py`）：完全不依赖 AI。撤销栈是整轨快照（`UNDO_DEPTH=50`）；
`GET /export/command` 只产出 ffmpeg 参数计划，`POST /export` 才真的起进程。

**素材描述 = 模型引用一个素材时唯一看得到的那句话**（迁移 `0021_asset_description`：
`asset.description` + `character.description`）。没有它，「引用这张图」在 prompt 里只剩一个
文件名，视频 prompt 根本构建不起来。整条链五处，各处的口径都只有一份：

- **能存 / 能改**：`services/assets.py::update()` 是 `Asset` 上**唯一的文本写路径**
  （`assign` 的 `allowed` 只放 `description` 过去——`path` / `kind` / `sha1` 是落盘事实，
  改了就和磁盘上那个文件对不上）；`PATCH /projects/{pid}/assets/{asset_id}`。
  **清空传 `''`**，`null` 是「这次不改」（与 `ShotPatch` 同一条口径）。
  `GET /assets/undescribed` 是「还缺哪些」那份清单，**临时资源不算**（`TRANSIENT_KINDS`：
  抽出来的帧、拆出来的音频），每条带 `owners` 说清它挂在谁身上。
- **进 prompt 只有三跳**：`services/context.py::_desc_of()`（唯一的取值口径：素材自己那句 →
  退回实体设定文字 → 空）给账单每条加 `desc` / `desc_missing` 并冻结进
  `GenerationVersion.context_json` → `services/generation.py::_images_of` 装进
  `RefAsset(desc=…)`（同时冻结成 `params.refs[].desc`）→
  `generation/providers/base.py::ref_hint()` 渲染成
  `参考素材说明：参考图1=阿岚（默认形象）（褪色军绿夹克…）。`。
  **截断只有 `clip_desc()` 这一处**（`DESC_MAX = 120`）：账单与冻结参数都留全文，界面才说得清
  「当时到底喂了哪句话」。**没有描述时 `ref_hint()` 的输出与升级前逐字相同**——老工程的
  prompt 不会因为加了这条链而变样（`tests/test_context_desc.py` 盯着）。
  最后那一跳**按 provider 分岔**：ComfyUI 那类图只按顺序收素材、收不到标签，所以只能拼进
  prompt；`http_api` 收得到结构化字段，描述就走 `refs[].desc`（合同里那一项），
  **不重复塞进 prompt** ——描述属于素材本身，混进提示词只会让服务端再解析一遍。
- **AI 能看图补**：`supports_vision` 是**协议级**事实，写在 `ai/llm/protocols.py::BY_NAME` 里
  （四种方言各一份图片编码：OpenAI `image_url` / Anthropic `image` 块**在文字前** /
  Gemini `inline_data` / Ollama `images[]` **纯 base64 不带 `data:` 前缀**），
  `describe_image()` **回纯文本**（一句描述套一层 JSON 只多一处能解析失败的地方）。
  `supports_vision=False` 的端走基类默认实现：四要素错误 + 手填那条出路（硬约束 2）。
  看图模型可以和主模型不同，设置项 `llm.vision_model`（留空 = 用主模型）。
- **建议只填输入框，落库只有「用户按保存」**：`services/describe.py` 的 `plan()`（只读、不出网）
  与 `suggest()`（出建议，**一行库都不改**）；`POST /projects/{pid}/describe/plan` / `.../suggest`。
  非图片素材在出网之前就跳过并说清原因，绝不把整段视频送出去。AI 协作栏那侧是
  `list_undescribed()` / `look_at_image()` 两个读工具 + 写工具 `set_description`（六种
  `target_kind`，`target()` 是唯一的目标解析：形象落 `traits`，其余落 `description`），
  照老规矩只翻译成提案，`POST /director/apply` 才落。
- **前端只有一个共用件** `shared/ui/AssetDescription.vue`，摆在五处：资产总账页、角色的定妆图、
  地点变体的参考图、道具的参考图、素材库。字数上限只认后端给的 `desc_max`，前端不写死 120。
  素材库那侧写的是**已有的 `note` 列**（库不走 alembic，加列会让已有的 `library.db` 打不开），
  且没有工程上下文，所以 AI 看图那颗按钮在库里是 disabled + 写清去哪儿做。
  采用时库里那句 `note` 会落进工程的 `asset.description`，**但已经有描述的不覆盖**——
  采用是单向复制，库不该回头盖掉工程里改过的话。
  账单上每条都显示这句话（`features/story/ShotView.vue` 与 `features/flow/SceneWorkbench.vue`），
  空的标「没有描述 · 模型只看到一个文件名」——判断用后端的 `desc_missing`，前端不算第二遍。

**生成层 = 两级场景系统 + 工程路由 + provider 适配层**（不再是「Workflow 为中心」）：

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
- **镜头上的首帧 / 末帧槽位**（`Shot.first_frame_asset_id` / `last_frame_asset_id`，迁移
  `0013_shot_frames`）：**「哪一张是首帧」是用户按下去的那一下**，不再由上下文账单自动提拔
  优先级最高的那一条（那条老规矩会把角色三视图当成画面第一格）。两列都可空，空 = 没有指定；
  `tail_frame` 衔接的镜头照旧用上游那张抽出来的真末帧（`prev_shot_id` +
  `services/frames.py`）。**必须是图片**：视频 / 音频报 422「首帧只能是图片」；
  **清空要传 `''`**（`ShotPatch` 走 `exclude_none`，`null` 会被当成「这次不改」）；
  **刻意不加外键**：资产被删掉时按「这个镜头没有首帧」处理，`GET /shots/{id}` 除两个 id 外
  还回 `first_frame_path` / `last_frame_path`，资产行已经不在了时是 `null`，界面显示
  「指定的图已不在」而不是画碎图标。老工程升上来只是多了两个空列，行为不变——真正的行为变化在
  `services/context.py::_assign_roles`（不再提拔参考素材），不需要迁移。
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
- **AI 协作栏**（`app/ai/director/` + `app/ai/skills/` + `services/director.py` +
  `api/director.py`，前端 `features/director/DirectorPanel.vue`——**剧本页与幕流程图页共用这一个
  组件，也共用同一个会话**，所以它不在任何一个 feature 目录下面）：`ai/director/tools.py` 里那条
  **读 / 写分界就是安全边界**——读工具（`list_*` / `get_scene` / `read_script` / `read_skill`）
  立刻执行，写工具（幕级 `add_scene` / `set_link` / … + 镜头级 `add_shot` / `update_shot` /
  `delete_shot` / `reorder_shots` / `set_shot_link`）**永不落库**，只翻译成一条提案
  `{op, target, temp_id, before, after, why, warnings}`。`chat` 一行库都不改，
  只有 `POST /director/apply` 才落，且只落 `op != "reject"` 的条目（照
  `story.propose_breakdown` / `apply_breakdown` 的老规矩），逐条转调已有的 `story` / `sequence`
  写方法——绝不另写一份写库逻辑。工具循环上限 `agent.MAX_ROUNDS = 16`；转满轮数时**先把提案落成
  `DirectorTurn` 记录再报错**，已产出的提案照旧可审阅。不支持 function calling 的端（Ollama）
  退化成一次性 `complete_json()`，提案形状完全一样。会话与提案存 `DirectorTurn`（只增不改），
  审阅到一半刷新页面不丢。
  - **拆长剧本靠分段读，不靠一次吐完**：`read_script(offset, limit)` 回
    `{total, offset, next_offset, done, text}`，模型自己一段一段读、每次只就读到的那一段提案，
    于是每一轮 chat 的输入输出都是有界的。老的一次性拆解（`POST /story/breakdown[/apply]` +
    `story.propose_breakdown`）**降级为兼容路径**：后端与它的测试原样保留，界面上已经没有入口
    了——一次调用要吐出全部幕 + 全部镜头 + 每镜的 prompt，长剧本必然超时或被截断。
  - **镜头 prompt 照内置 SKILL 写**（`app/ai/skills/video_prompt.py`，四份 `flf` / `i2v` /
    `l2v` / `ref` 对应四种首尾帧形态，详见 docs/05）：**渐进披露**——只有 `catalog()` 那几行
    进系统提示词，全文由 `read_skill(name)` 按需取一份。写工具收的是三段字段
    （`camera_motion` / `visual_prompt` / `audio_dialogue` / `negative_prompt` + `skill`），
    最终那段正向 prompt 由**已有的** `prompts.format_shot_prompt()` 再过
    `with_shot_audio_policy()` 拼出来——**无配乐那条硬约束只有一处口径**，SKILL 里的
    `non_diegetic_music` 一节固定写「无配乐」而不是再实现一遍。`update_shot` 没给的那几段从
    库里现有 prompt 用 `parse_shot_prompt()` 解析出来补上，改一段不会把机位与对白擦掉。
  - **`scope` 只是一句提示**（`chat(pid, message, scope)`，`script` / `flow`）：只影响这一次
    请求拼出来的系统提示词里那一句「用户现在在哪一页」，**不落库、不分会话、不需要迁移**——
    换页不该让历史对话变味。剧本页右栏是「AI 编剧」Tab（`scope="script"`），
    幕流程图页右栏是「AI 协作」（`scope="flow"`），两边看到的是同一份对话。
  - **拆完一段之后要对一遍账**（读工具 `list_missing_materials` + 素材级写工具
    `add_character` / `add_location` / `add_prop` / `generate_reference`）：拆出来的镜头里那些
    人名、地名一个都没有对应素材时，每个镜头只喂得进一句文字，人物形象在几秒里就丢了，
    而这件事**在剧本页上完全看不出来**（幕与镜头都好端端地立着）。那张账的四类
    （形象缺定妆图 / 地点变体缺参考图 / 道具缺参考图 / 幕缺人缺地点）**全部转调已有判断**
    ——幕那一类直接读 `story.storyboard()` 的 `context_issues`，「出图这条链配没配」读
    `images.capability()`，绝不在这里再算一遍。没配出图服务时账上先说这件事
    （`image.configured=false` + `how_to`），素材照旧建、图这一项走手动那条路（硬约束 2）。
  - **同一批里新建的素材按名字接线，且与提案顺序无关**（`services/director.py::_Batch` +
    `_wire_pending`）：`add_shot` 完全可能排在 `add_character` 前面（顺序由模型定），
    所以提案阶段对不上的名字只留成 `pending_name`（**一行库都不改**），整批落完再统一接一次。
    接线**转调已有写方法且写的是「该有的全量」**——`set_shot_cast` / `set_shot_props` 是整份
    覆盖而不是追加，只写新建那几个会把先落的冲掉。**接不上只是接不上**：用户把那条
    `add_character` 丢掉时，那一镜照样落库，只在它自己的落库回执里多一句
    `cast_wired` / `cast_skipped`（道具 `props_*`、地点 `location_*` 同理，措辞只有
    `_wire_pending` 一处），绝不让一条被丢弃的提案带走整个镜头。
    **前端必须把这几句显示出来**（`DirectorPanel.vue::appliedRows`）：落成了的那张提案卡会
    走掉，只给一行「已落库 N 条」等于把降级藏起来（硬约束 4）。
- **工程路由**（`services/route.py` + `GET /projects/{pid}/route`，迁移 `0022_project_route`）：
  「**这个工程 + 这个能力 → 走哪条路、这条路要绑什么、绑没绑上、缺什么**」全应用只有这一份口径，
  概览页、Workflow 管理页那条横幅、二次处理弹窗、入队守卫读的都是它。六条不许绕的：
  - **调用方式是工程级可继承的一列**（`project.generation_mode`）：**空串 `''` = 跟随设置页**
    （绝大多数工程是这一种，改设置页就跟着变），显式选一条之后就不再跟。所以
    `summary()` 同时给 `provider`（最终走哪条）与 `source`（`project` / `settings` / `default`，
    是谁给的这个答案）——「跟随设置页」和「谁都没选过用的是代码里那个默认值」对用户是一回事，
    排查时方向不同，所以分开说。文案只有一份：`ROUTE_SOURCE_LABEL`。
  - **界面照 `binds` 分岔，不照调用方式的名字**（硬约束 1）：`preset` 显示两份预设选择器 /
    `base_url` 显示服务地址 / `workflow` 显示四个能力各一份图。`BINDS` 那张表是唯一真源，
    前端一个 provider 名字都不写死；未知名字是空串（「什么都不用绑」）。
  - **readiness 是按能力算的**，不是按工程：同一个工程 `image2video` 可以是 ready 而
    `first_last_frame` 缺一份图。`capabilities[]` 每条自带 `ready` + `issues`（四要素，
    `suggestions` 原样显示）+ `slots`（一次能喂几个参考素材，**`null` = 不限制，`0` 是有意义的
    答案**，两者不能都画成「—」）。
  - **读路径绝不抛**：`resolve()` / `capacity()` / `summary()` 缺什么都写进 `issues` 照常返回
    （概览页要能画出「缺什么」这张图）；**入队那道门是 `require()`**，它把 `issues[0]` 抛出来。
    编出来的调用方式名走 `normalize()` 报 `VALIDATION_ERROR`。
  - **入队解析一次并冻结，执行与重试只读冻结值**：`Route.frozen()` 落进
    `job.params["route"]`，`generation._provider_of()` 三级回退（`params.route.provider` →
    `params.generation_mode` → 应用级设置，最后一级只为这次改造之前入队的老任务）。
    中途在设置页改了调用方式，「重试」不该变成「换个后端跑一遍」（硬约束 3）。
    冻结的**只有事实，没有当时的 readiness**——`ready` / `issues` 说的是解析那一刻缺什么，
    冻进去只会让半年后翻参数的人把它当成这次任务的失败原因。**地址进档，密钥永不进档。**
  - 老任务里没有这一项，所以前端 `shared/api/projects.ts::frozenRoute()` 回 `null` 而不是替它
    编一条「ComfyUI 预设」——谎报走了哪条路正是这次要修的 bug 的形状。
- **provider 适配层**（`app/generation/providers/`）：`base.py` 定义与模型无关的 `VideoRequest`
  （`mode` = `i2v` / `flf`、prompt、首尾帧、**参考素材 `refs`**（`RefAsset`，带 `media` =
  `image` / `video` / `audio`）、时长、seed、透传 `extra`、降级说明 `notes`）与 `VideoProvider`
  协议（`probe` / `submit` / `poll` / `fetch`）。**三条正经路，谁都不是兼容路径**：
  `comfy_preset.py`（默认核心，照节点标题约定注入）· `http_api.py`（通用 REST 合同）·
  `comfy_workflow.py`（按你自己那份图的绑定表填，绑定表来自 Workflow 管理页）。
  `registry.py::provider()` 按名字取一个适配器，**那个名字由工程路由给**（入队时
  `route.require()` 解析并冻结，见上一条），不再由业务层写死 `provider("comfy_preset")`。
  不传名字才回退到应用级设置——只有设置页那颗「测试连接」用得上（那里没有工程上下文）。
  **本工具不维护模型端的图**：ComfyUI 适配器只按**节点 title 约定**注入入口参数——
  `AIVS_FIRST_FRAME` / `AIVS_LAST_FRAME` / `AIVS_PROMPT` / `AIVS_NEGATIVE` / `AIVS_DURATION` /
  `AIVS_SEED` / `AIVS_REF_1`…`AIVS_REF_9`（参考图，最多 9 个）/
  `AIVS_REF_VIDEO_1`…`AIVS_REF_VIDEO_4`（参考视频，最多 4 个）/
  `AIVS_REF_AUDIO_1`…`AIVS_REF_AUDIO_4`（参考音频，最多 4 个）——不解析、不校验、不改写图里的
  lora 与加速节点。缺必需 title 时报 `INVALID_WORKFLOW`，建议里写「在 ComfyUI 里把该节点标题改成
  X」。lora、加速节点、采样器怎么摆是模型端自己的事，本工具跟着改迟早两边打架。
  **标了标题却这一次没有值时，媒体入口连节点一起摘掉、标量保持原值**
  （`comfy_preset._detach_idle` / `comfy_workflow._detach_idle` → `comfy/graph.py::detach`，
  两张分界表 `presets.MEDIA_MARKERS` 与 `graph.MEDIA_SLOTS`）：图里那一格存的不是空值，而是用户在
  ComfyUI 里存图时挂着的**示例文件**，留着就等于把一张不相干的图真喂进模型——画面往它上面收敛，
  而队列里一条错误都没有，于是「多标几个入口」反过来成了风险，用户不敢在图里多摆节点。口径是
  **标了 `AIVS_*` = 这一格由本工具填，本工具这次没填 = 这一格这次不用**；标量相反
  （seed / 时长 / 宽高 / 负向），保持图里原来的值才对——那是用户有意存进去的默认参数。
  摘节点**只跟着连线走**（不认识任何 `class_type`，照旧不 import 服务层、不打 `/object_info`）：
  切掉指向被摘节点的输入之后**一条连线输入都不剩**的下游节点是只为它服务的中间件，跟着摘；还连着
  别的线的是汇合点（`WanImageToVideo` 丢了 `end_image` 还连着 `positive` / `vae` / `start_image`），
  到此为止；这一次真填了值的入口节点走 `keep=` 保护。摘了什么写进 `req.notes` → 冻结成
  `params.ref_notes`（硬约束 4）。**唯一残余风险**：被摘的那一格在图里是必填的
  （`ImageBatch.image1`），此时 ComfyUI 拒绝提交，`comfy_base.detached_submit_error` 在原错误上补两
  条点名建议——**只在真摘过、且错误真是 `WORKFLOW_ERROR` 时补**，离线 / 超时那类失败加这两句只会
  把真正的原因埋掉。
- **首尾帧 ≠ 参考素材**（`AIVS_REF_*` 三族就是为这件事加的）：首尾帧决定「画面从哪一格开始 /
  结束」，参考素材决定「谁出场、在哪儿、什么动作、什么声音」。只喂一张首帧最容易丢的就是
  人物形象，所以账单里采用的条目**除首帧那一张之外全部当参考素材送到模型端**
  （`generation._images_of`）。**槽位不够只降级、不失败**：图里标了 3 个而账单给了 5 张就填
  前 3 张，把少喂了哪几张写进 `req.notes` → 冻结成版本参数 `ref_notes`（`refs` 记实际喂了
  哪几张），界面上看得见。**反过来槽位多余就摘掉**：标了 9 个而这个镜头只有 2 张时，剩下 7 个槽位
  连节点一起从提交的副本里摘掉（见上一条），所以标多了不再有代价。
  一个 `AIVS_REF_*` 都没有的预设照样 `ready`，只是设置页的预设列表会把
  「参考图 0 槽」标成警告；**参考视频 / 参考音频 0 槽是常态**（绝大多数图只收图片），只在真标了
  槽位时画徽标，否则会把前面那个真问题埋掉。默认会在 prompt 末尾附一句
  `参考图说明：参考图1=…`（`base.ref_hint`，ComfyUI 那类图收不到标签，只能靠这句对号），
  设置里 `video.ref_labels` 可关。**三族分开算槽位**：把 `.mp4` 接到 `AIVS_REF_1` 上会喂给
  LoadImage 一个视频文件名，既不报错也出不了片。
- **第三条生成链：图片**（`app/generation/providers/image.py` + `services/images.py` +
  `api/images.py` + `ai/skills/image_prompt.py`，前端只有一个共用弹窗
  `features/images/GenerateImageDialog.vue`）。视频与音频之外的第三条，出的是**素材图**：
  角色四视图 / 地点变体参考图 / 道具图 / 镜头首末帧候选。五条不许绕的：
  - **协议表是唯一真源**，和 `ai/llm/protocols.py` 同一个形状：`none` / `comfy_preset` /
    `openai_images` / `gemini` / `http_api` 五项写在一张 `BY_NAME` 里（默认地址、要不要密钥、
    收不收参考图、模型列表从哪来），`GET /settings` 投影成 `image_protocols[]` 给前端画界面
    ——加一家 API 只改那一个 dict，前端一行不动。**密钥只走请求头**（Gemini 刻意不用 `?key=`），
    出网只有 `image._client()` 这一个口子，测试全靠 monkeypatch 它关在机器里跑。
  - **结构由内置 SKILL 补，用户那段话只写「长什么样」**（`ai/skills/image_prompt.py` 三份：
    `char_sheet` / `scene_simple` / `prop_ref`）。拼装口径只有一处
    `render_image_prompt(name, user_text)`，AI 路径与手动按钮共用；四视图、纯背景、无文字
    那几句是系统追加的，模型与用户都改不到。`read_skill` 一个工具同时查视频与图片两张表。
  - **不新造队列**：图片任务就是一行 `Job`（`kind` ∈ `t2i` / `i2i`），靠 `0020_image_jobs`
    的可空 `shot_id` + `target_kind` / `target_id` 挂到出图对象上，轮询 / 取消 / 重试 / 优先级
    全部继承 `generation._await_task()`。底部控制台靠 `list_jobs()` 多回的 `target_label`
    显示「角色 · 阿岚 四视图」，不是空白。
  - **落地全部转调已有写方法**（`cast.add_sheet` / `world.add_variant_reference` /
    `world.add_prop_reference`，都是 append-only，旧版本一条不删），`services/images.py` 自己
    不碰 ORM。**镜头首末帧只进素材库、绝不写槽位**——「哪一张是首帧」只认用户按下去的那一下。
  - **先账单再动手**：`POST /images/plan` 只读地给出用哪个协议、照哪份 SKILL、拼出来的
    正 / 负向 prompt 全文、图会落到哪里、缺什么；弹窗上也照这个顺序，`can_generate=false`
    时把四要素错误连 `suggestions` 一起摆出来。出图服务没配置时 AI 那条提案**照旧建素材**，
    只把跳过的原因写进 `warnings` / `applied[].image_skipped`。
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
  环境变量」。`POST /settings/probe` 分别探 LLM、视频与**图片**服务（`what` ∈
  `llm` / `video` / `image`），`POST /settings/models` 也是这三族共用，靠 `field.fetch`
  认「这一项属于哪一族」——它同时就是设置键前缀，所以前端不写第二张对照表。
  出图那一族是 `image.*` 七项（`provider` / `base_url` / `model` / `api_key` / `preset` /
  `size` / `timeout`），`provider` 的候选直接来自协议表。**API key 永不回明文**：只回
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
- **「Workflow 管理」是高级页面，但它管的那条路不是兼容路径**（`features.ts` 里 `advanced: true`、
  不进 `PROJECT_NAV`，从命令面板或设置页进）：它是 `comfy_workflow` 那条路**绑图的地方**——
  一个能力绑一份图 + 一张字段绑定表（`services/workflows.py`，纯函数下沉在
  `app/generation/comfy/graph.py`：`SLOTS` / `parse_graph` / `apply_bindings`）。
  工程没选这条路时四个能力下拉是禁用的，**判据是 `binds_workflow` 而不是调用方式的名字**，
  且旁边必须写清为什么禁用。执行侧 `_execute` **不再按 `job.workflow_id` 分支**
  （老的 `_run_legacy` 整个删掉了，那支从来没被触发过，等于选了「工作流绑定」什么都不会发生）：
  装不装 `WorkflowSpec` 判的是「这个任务有没有绑定的图」这个事实（`_workflow_spec_of`，
  id 来自入队冻结的 `params.route.workflow_id`），提交由 `providers/comfy_workflow.py` 做。
  **图与绑定表刻意不进 `params_json`**——一份 api_json 动辄几十 KB，每个版本存一份会把工程库
  撑起来，冻结的是 id。

**新手引导与演示工程**（`services/onboarding.py` + `api/onboarding.py` + `core/pngdraw.py`，
前端 `features/onboarding/`）：第一次打开应用的人面前不该只有「新建 / 打开」两个按钮，
所以有一份能立刻点开看的演示工程 + 一个五步向导（这是什么 → 演示工程 → 连上生成服务 →
绑定预设或 API → 功能巡览）。六条不许绕的：

- **状态是应用级的**：落 `settings.runtime_dir / "onboarding.json"`（与 `recent.json` /
  `library.json` / `settings.json` 同级），坏 JSON 照 `appsettings._read()` 退回默认并留日志。
  `first_run` 就是「这个文件还不存在」；**关掉 ≠ 走完**，`completed` 只有点「完成」才写。
- **演示工程由后端代码播种，不往仓库塞二进制**（schema 永远是当前最新），落用户文档目录下的
  `AI Video Studio/演示项目`（安装目录在 Windows 上常常只读），重名加 `-2`。
  **播种全部转调已有写方法**（`cast` / `world` / `assets` / `story` / `sequence`），
  `_seed()` 自己一行 ORM 都不碰——所以**不新增表、不新增迁移、`schema_version` 不动**。
- **先账单再动手**（照 `services/adopt.py` / `services/packages.py`）：`POST /onboarding/demo/plan`
  一个字节都不写；目录里已经有工程时 `action="open"`，点下去只打开、不重建、不覆盖。
  陌生 `project.db` 由 `projects.create()` 现成的 `CONFLICT` 挡住，这里不另写一份。
- **演示工程里刻意没有任何 `GenerationVersion`**：版本轨与时间线是空的，`warnings` 与界面都
  写明「配好生成服务后从这里做出第一段画面」。一个看着能播其实是假的演示比空版本轨更糟。
  `tests/test_onboarding.py` 盯着每张分镜卡 `version_count == 0`。
- **占位图纯 Python 画**（`core/pngdraw.py`：`struct` + `zlib` 手写 PNG，理由同
  `scripts/make_icons.py`——打包机不必装 Pillow），**不画文字**（字形超出范围）：
  「这是谁」由那句 `description` 说清楚，而描述才是模型真正看得到的东西，所以每张占位图都
  写了描述，播完 `GET /assets/undescribed` 必须是空的。
- **向导不进 `app/features.ts`**：它是覆盖层不是页面，登记进注册表会让它出现在导航与它自己的
  巡览列表里（自我指涉）。它挂在 `WorkbenchLayout` 里与 `CommandPalette` 同级常驻，重开入口
  三处：设置页顶部、命令面板、起始页最近列表为空时（后者直接落在演示工程那一步）。
  巡览那一步的文案**一个字都不在前端写**，全部来自 `features.ts` 的
  `purpose` / `outcome` / `requires`，所以以后加功能会自动出现在巡览里。
  **启动时不播种**：启动不该悄悄往用户文档目录写东西，播种是向导里那一下点击。


## 代码约定

- **id**：`new_id("shot")` → `sht_<ULID>`。新实体必须先在 `app/core/ids.py` 的 `PREFIX` 里登记，
  否则直接抛 `ValueError`。
- **时间**：一律是 `utc_now()` 产出的 ISO 字符串（`String(40)` 列），不用 DateTime 类型。
- **JSON 列**：叫 `*_json` 的 Text 列存 JSON，读一律走 `load_json(raw, fallback)`
  （坏 JSON 退回默认值并保持可用，不抛）。对外输出时把 `*_json` 展开成干净字段再返回。
- **新增表**：工程表必须在 `persistence/all_models.py` 里 import，否则 `Base.metadata` 漏表；
  素材库表相反——挂 `LibraryBase`，**不要**进 `all_models.py`（理由见上面的素材库段）。
- **新增迁移**：`alembic/versions/` 加脚本 → 在 `persistence/migrate.py::REVISION_SCHEMA` 登记
  它对应的 schema 版本 → 同步 `settings.schema_version`（当前 22，最新一条是
  `0022_project_route`：`project.generation_mode` 改**可空、默认 `''`**——空串 = 跟随设置页，
  同时把老库里的 `workflow_api` 归一成 `comfy_workflow`、把等于旧默认值 `comfy_preset` 的行清成
  空串（这一列在此之前从未被读过，所以不丢用户意图），见上面的「工程路由」段；
  **`job` / `generation_version` 一列都不动**——那条路是入队时冻结进 `job.params_json["route"]` 的，
  不是新列。上一条 `0021_asset_description` 是 `asset.description` + `character.description` 两列，
  见下面的「素材描述」段；再上一条 `0020_image_jobs` 是图片任务——它不挂在镜头上，所以
  `job.shot_id` 改可空，另加 `target_kind` / `target_id` 两列指向出图对象（`appearance` /
  `location_variant` / `prop` / `shot_first_frame` / `shot_last_frame`）；`GenerationVersion` 一列
  不动——素材图的「永不覆盖」由 `SheetVersion` / `LocationReference` / `PropReference` 已有的
  `version_no` + `is_current` 保证）。漏登记会导致打开旧工程时无法告诉用户「schema X → Y」。
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
  用 `POST /shots/{id}/versions` 手工造版本。**再要用 `video_preset` fixture**：入队门槛在
  `route.require()`（这条路绑没绑上），缺预设时按下生成立刻是四要素错误、根本排不进队列——
  队列机制 / 编排 / 批次那些用例测的不是这道门槛，所以让 fixture 一次把前提摆齐。
- conftest 提供 `error_of`（断言错误四要素齐全）、`ready_workflow`（导入 + `validate?probe=false`，
  本地绑定校验不需要 ComfyUI）、`video_preset` / `write_preset` / `PRESET_GRAPH`（摆一份
  `AIVS_*` 标题齐全的预设图并让设置页指向它，**走 `app_settings.patch()` 真落
  settings.json**——`TestClient` 起 lifespan 时会再 `apply()` 一遍，monkeypatch 单例会被擦回
  默认值）、`upload_png`、`GRAPH` / `BINDINGS`；素材库侧是 `library`
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
+ 落盘规范 + 测试清单（`2.18` 工程路由那一列 / `3.4` Route 的形状与冻结） · `docs/04` Step 1–9
与完成标准 · `docs/05` 三条路与「这个工程走哪一条」（`AIVS_*` 节点标题约定、http_api 合同、
绑定表、最小验收清单）· `docs/06` 打包与分发（五步流水线、各平台前置、冻结后的路径规则、
sidecar 那三个坑）。service / api 的 docstring 里写的「Step N」对应 `docs/04`；改接口或表结构时
同步 `docs/03`，改生成层的入口约定或适配器时同步 `docs/05`，改打包脚本 / spec /
`tauri.conf.json` 时同步 `docs/06`。

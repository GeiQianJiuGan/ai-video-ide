# AI Video Studio

**简体中文** · [English](README.en.md)

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

桌面端优先的 AI 原生长视频制作工作台。

> **AI = 素材生产器 · System = 视频工程与编排器 · Human = 导演**

AI 能出好看的几秒钟，却拼不出一部片子——人物长相会漂、镜头接不上、改一处要重跑一整批。
AIVS 不做模型，只做那个把「几秒钟」组织成「一部片子」的工程系统：角色形象固定下来、
喂给模型的东西明确到每一格、每次生成都留一版永不覆盖、时间线与导出完全不依赖 AI。

## 它是什么 · 它不是什么

| 是 | 不是 |
|---|---|
| 视频工程与编排器：管角色、幕、镜头、上下文、版本、时间线 | 不是模型，也不训练模型 |
| ComfyUI / 通用 REST 服务的调用方 | 不维护、不解析、不改写你的 ComfyUI 图 |
| 本地优先：一个工程 = 一个自包含目录，可整体拷走换机继续 | 不是云服务，不上传你的素材 |
| LLM 可选：手动模式能走完整流程 | 不是「必须先接大模型」的工具 |

## 四条硬约束

1. **业务层不绑定具体视频模型** —— 不存在 `if model == "wan"`，差异全部下沉到 provider 适配层。
   镜头上只写能力（`text2image` / `image2video` / `first_last_frame` / `upscale`）与 provider 名。
2. **LLM 不是必选项** —— 默认 `llm_provider="none"`，AI 入口返回 `LLM_UNAVAILABLE` 并写明手动路径。
   Source of Truth 始终是工程目录里的 `project.db`。
3. **生成版本永不覆盖** —— `GenerationVersion` 只增不改，冻结当次 prompt / 上下文 / 参数 / 产物。
   没有任何接口能改写已存在的版本，只能换「当前采用的是哪一版」。
4. **绝不静默失败** —— 每个错误都是四要素 `{code, title, detail, suggestions}`，
   界面必须把建议显示出来。启动失败也绝不白屏。

## 核心链路

```text
Character → Appearance → Scene → Shot → Context → Generation
         → GenerationVersion → Clip → Timeline → Final Video
```

这条链不可跳跃：想生成一个镜头，它的上下文账单必须先完整（谁出场、在哪儿、从哪一帧开始）。

## 使用流程（第一次用）

1. **起环境**：`python scripts/dev.py`（见下面「快速开始」）。
2. **新建工程**：选一个空目录，落下 `project.aivs.json` + `project.db`。整个目录可以随时拷走。
3. **准备一份生成预设**：在 ComfyUI 里把入口节点的**标题**改成 `AIVS_PROMPT` /
   `AIVS_FIRST_FRAME` / `AIVS_REF_1`… → 用「Save (API Format)」导出 → 在设置页上传并设为默认。
   本工具只按标题往里填值，图里的 lora、加速节点、采样器一概不看不改（见 [docs/05](docs/05-生成方式与节点要求.md)）。
4. **固定形象**：角色 → 外观 → 角色表；地点 → 日夜雨雪变体 → 参考图。这一步决定后面几十个镜头长得一不一样。
5. **拆剧本**：粘贴一段文字，AI 拆成幕与镜头，或者完全手动建——两条路等价。
6. **配镜头**：在分镜板上给镜头指定首帧 / 末帧、在卡片之间连转场线、打开上下文检查器确认
   「到底会喂什么给模型」（每一条都带 included / reason）。
7. **生成**：入队后在底部控制台（`Ctrl` + `` ` ``）看队列与日志。每次生成都留一版，挑一版采用，旧版一条都不删。
8. **成片**：时间线自动装配 → 手工微调 → 导出。这一步只用 FFmpeg，ComfyUI 和 LLM 都不需要在线。
9. **换机器继续**：起始页「导出当前工程」把工程打成一个 `.aivspkg`（默认带素材、不带成片），
   在另一台机器上「导入工程包」还原。想只搬一幕的设定（人物 / 地点 / 道具 / 镜头结构）就用
   幕流程图上的「导出这一幕」/「导入一幕」，能导进任意一个已打开的工程。
   **包里不带预设图，也不带任何密钥与服务地址**——只带一份「这个工程要什么」的环境清单，
   导入前会逐条告诉你本机缺什么。

更详细的分步说明见 [docs/04](docs/04-功能开发步骤与体验.md)。

## 环境要求

| | 是否必需 | 说明 |
|---|---|---|
| Python | 3.11+ | 后端（FastAPI + SQLite） |
| Node.js | 20+ | 前端（Vue 3 + Vite + TS） |
| FFmpeg | 随应用分发 | 抽帧 / 转码 / 导出。`python scripts/fetch_ffmpeg.py` 下载到 `bin/`，不必自己装 |
| ComfyUI | 可选 | 离线时素材整理、分镜、时间线、导出照常可用，生成按钮会说明原因 |
| LLM | 可选 | 默认不启用；剧本拆解与 AI 导演栏都有对应的手动路径 |
| Rust | 仅打桌面壳时 | `cargo install tauri-cli --version "^2"` |

## 快速开始

```bash
python scripts/dev.py
```

一条命令起后端（`127.0.0.1:8765`）+ 前端（<http://localhost:5173>），做一次依赖体检，
起好自动开浏览器，`Ctrl+C` 一起停。Windows 上也可以直接双击 `start.cmd`，macOS / Linux 用 `./start.sh`。
常用参数：`--backend-only` / `--frontend-only` / `--port 8899` / `--no-open`。

首次克隆后先装依赖：

```bash
cd backend && python -m pip install -e ".[dev]"
```

```bash
cd frontend && npm install
```

```bash
python scripts/fetch_ffmpeg.py
```

少了哪一步，启动脚本会直接告诉你该敲什么。

### Docker（把编排端放到 Linux 服务器）

ComfyUI 通常在另一台有显卡的机器上，所以容器里只跑前端 + 后端 + FFmpeg：

```bash
cp .env.docker.example .env && ./docker-start.sh
```

在 `.env` 里把 `AIVS_COMFY_BASE_URL` 指向你的算力机。完整说明见
[docs/docker-deployment.md](docs/docker-deployment.md)。

## 生成方式：只认节点标题

本工具**不维护模型端的图**。ComfyUI 适配器只按节点标题注入入口参数，不解析、不校验、不改写：

| 标题 | 作用 |
|---|---|
| `AIVS_PROMPT` / `AIVS_NEGATIVE` | 画面提示词 / 负向提示词（`AIVS_PROMPT` 是出画面唯一的必需入口） |
| `AIVS_FIRST_FRAME` / `AIVS_LAST_FRAME` | 首帧 / 末帧。严格首尾帧（补转场）需要两个都标 |
| `AIVS_REF_1` … `AIVS_REF_9` | 参考图槽位：角色表、地点参考图从这里进去 |
| `AIVS_REF_VIDEO_1..4` / `AIVS_REF_AUDIO_1..4` | 参考视频 / 参考音频，与图片分开算槽位 |
| `AIVS_DURATION` / `AIVS_SEED` | 时长（帧数）/ 随机种子 |
| `AIVS_SOURCE_VIDEO` | 二次处理（超分 / 插帧）的输入：已经出好的那一段 |
| `AIVS_AUDIO_TEXT` / `AIVS_AUDIO_PROMPT` / `AIVS_VOICE_REF` | 音源那份图的入口（声音是独立的一条链） |

**首尾帧 ≠ 参考素材**：首尾帧决定画面从哪一格开始 / 结束，参考素材决定谁出场、在哪儿、什么动作。
槽位不够只降级不失败，少喂了哪几张会写进版本参数，界面上看得见。

除 ComfyUI 之外还有 `http_api`（通用 REST 合同）。合同细节与最小验收清单见
[docs/05](docs/05-生成方式与节点要求.md)。

## 配置

三层，优先级从高到低：**设置页写的 `settings.json` → `AIVS_` 环境变量（含 `backend/.env`） → 代码默认**。
每个字段在设置页都会显示它当前的值来自哪一层。

常用环境变量：`AIVS_PORT`、`AIVS_COMFY_BASE_URL`、`AIVS_FFMPEG_PATH`、`AIVS_WORKER_LIMIT`、
`AIVS_LLM_PROVIDER`、`AIVS_RUNTIME_DIR`；前端代理目标用 `AIVS_BACKEND` 覆盖。
API Key 在接口里永不回明文，只回掩码与「有没有值」。

## 目录结构

```text
xunjie_video_ide/
├── backend/          Python + FastAPI + SQLite（api → services → persistence 三层）
│   ├── app/api/          极薄路由层：Pydantic body + 转调
│   ├── app/services/     业务层，每个模块导出一个单例
│   ├── app/generation/   provider 适配层（ComfyUI 预设 / 通用 REST）+ 预设解析
│   ├── app/ai/           LLM 协议适配层 + AI 导演（写工具只出提案，不落库）
│   └── alembic/          按工程库跑的迁移
├── frontend/         Vue 3 + Vite + TS（features/ 按功能分，shared/ 是公共件）
├── tauri/            Tauri 2 桌面壳，以 sidecar 方式托管后端
├── scripts/          dev.py（一键起环境）· fetch_ffmpeg.py（下载内置 FFmpeg）
├── docker/           容器化部署（编排端上服务器，ComfyUI 留在算力机）
├── docs/             设计文档（中文）
└── bin/              内置 FFmpeg / FFprobe（不进 git）
```

工程数据**不在这个仓库里**：每个工程是用户自选的一个目录，含 `project.aivs.json` + `project.db`
+ `assets/` + `generations/` + `cache/`，没有全局数据库。

## 文档

| 文档 | 内容 |
|---|---|
| [01 技术栈与架构](docs/01-技术栈与架构.md) | 选型与取舍、进程架构、生成链路、模块边界、磁盘布局、风险对策 |
| [02 功能开发文档](docs/02-功能开发文档.md) | 信息架构、页面清单、功能规格与验收、状态机、M0–M6 里程碑、设计语言 |
| [03 数据模型与接口契约](docs/03-数据模型与接口契约.md) | 全量表结构、错误契约、REST / WS 接口、调度器规范、落盘规范、测试清单 |
| [04 功能开发步骤与体验](docs/04-功能开发步骤与体验.md) | Step 1–9 与每一步的完成标准 |
| [05 生成方式与节点要求](docs/05-生成方式与节点要求.md) | 三种调用方式、`AIVS_*` 标题约定、REST 合同、最小验收清单 |
| [Docker 部署](docs/docker-deployment.md) | Compose 与单容器两种方式、外部 ComfyUI 的接法 |

文档目前只有中文版。

## 开发

```bash
cd backend && python -m pytest -q
```

```bash
cd backend && python -m ruff check . && python -m ruff format .
```

```bash
cd frontend && npm run typecheck && npm run lint && npm run build
```

迁移是**按工程库**跑的，没有全局数据库：

```bash
cd backend && python -m alembic -x db=<工程目录>/project.db upgrade head
```

改动前请先读 [CLAUDE.md](CLAUDE.md)：它写清了四条硬约束的落点、各层的职责边界，
以及一堆「为什么这里刻意不那样写」的理由。

## 现状

- **后端与前端已全量接通**：23 个 service + 21 个 router（含 `/ws`），后端测试 352 passed。
  界面上登记的功能都真的连着后端，单个能力缺失（ComfyUI 离线、没配 LLM）时按钮保持禁用并说明原因，
  绝不画假界面、不造假数据。
- **桌面安装包还没打出来**：`tauri.conf.json` 已经把 Windows / macOS / Linux 三个目标写好了，
  但 Python sidecar 的构建脚本尚未落地，所以目前只有开发期跑法。
- **Linux**：后端与 Docker 路径已在用；桌面壳的 AppImage 目标尚未验证。

## 许可

本项目以 **MIT** 许可发布，见 [LICENSE](LICENSE)。用、改、闭源商用都可以，唯一的义务是
保留那份版权与许可声明。

**随应用分发的 FFmpeg / FFprobe 不在这个许可之下。** 它们是第三方静态构建、开了
`--enable-gpl`，因此二进制本身是 GPL 的。本应用只把它当外部进程调用（`app/core/ffmpeg.py`
是唯一入口），不链接它的库，所以两者是聚合分发，MIT 的本体代码不受影响——但**分发安装包的人
要履行 GPL 的义务**（随包提供许可全文与对应源码的获取途径）。细节与来源见
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)；不想附带 GPL 二进制时，跳过
`scripts/fetch_ffmpeg.py`、让用户自己装并用 `AIVS_FFMPEG_PATH` 指过去就行。

ComfyUI、模型权重与工作流图都**不由本项目分发**，许可归各自发布方。





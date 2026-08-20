# AI Video Studio · 设计文档

桌面端优先的 AI 原生长视频制作工作台。

> **AI = 素材生产器 · System = 视频工程与编排器 · Human = 导演**

## 文档

| 文档 | 内容 |
|---|---|
| [01-技术栈与架构](docs/01-技术栈与架构.md) | 选型表与取舍理由、进程架构、生成链路、前后端模块边界、磁盘布局、风险对策 |
| [02-功能开发文档](docs/02-功能开发文档.md) | 信息架构、路由与页面清单、24 个模块的功能规格与验收、状态机、M0–M6 里程碑、非功能需求、设计语言 |
| [03-数据模型与接口契约](docs/03-数据模型与接口契约.md) | 全量表结构、GenerationRequest / ResolvedContext / 错误契约、REST 与 WS 接口、调度器规范、落盘规范、测试清单 |

## 核心链路（不可偏离）

```text
Character → Appearance → Scene → Shot → Context → Generation
        → GenerationVersion → Clip → Timeline → Final Video
```

## 计划目录结构

```text
xunjie_video_ide/
├── docs/            设计文档
├── frontend/        Vue 3 + Vite + TS
├── backend/         Python + FastAPI + SQLite
├── tauri/           Tauri 2 桌面壳（sidecar 托管 backend）
└── workflows/       内置 ComfyUI Workflow + 能力声明
```

## 三条硬约束

1. **业务层不绑定任何具体视频模型**——差异全部下沉到 Workflow Adapter。
2. **LLM 不是必选项**——Manual 模式下全流程可用，Source of Truth 始终是数据库。
3. **生成版本永不覆盖**——每个版本冻结 Prompt、Workflow、Context、参数与产物。

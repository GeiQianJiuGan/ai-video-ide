# Tauri 桌面壳

壳只做三件事，不承载任何业务逻辑：

1. 启动 Python 后端 sidecar（`AIVS_PORT=0` 随机端口，只监听 `127.0.0.1`）。
2. 生成一次性握手 token，通过环境变量注入后端，再随 `window.__AIVS_ENDPOINT__` 注入前端；
   壳不从文件里读取 token，只用文件里的 token 做一致性校验，避免读到上一次的残留。
3. 启动失败时打开 `boot-error.html`，完整呈现 `code / title / detail / suggestions`——**绝不白屏**。

## 文件

| 文件 | 作用 |
|---|---|
| `src/backend.rs` | sidecar 生命周期：解析可执行文件、注入环境、等待 `endpoint.json` + TCP 探活、超时与崩溃诊断、退出时杀进程 |
| `src/main.rs` | 运行时目录选择、窗口装配、失败降级到启动失败页 |
| `tauri.conf.json` | 构建钩子、CSP（生产严格 / 开发放行 Vite）、externalBin 声明 |
| `capabilities/default.json` | 最小权限：只有 `core:default`，不启用 shell / fs / http 插件 |
| `../frontend/public/boot-error.html` | 启动失败页（外链 `boot-error.js`，因为 CSP 只允许 `script-src 'self'`） |

## 后端进程解析顺序

1. 与主程序同目录的 `aivs-backend[.exe]`（Tauri `externalBin` 的落点，打包后走这条）。
2. 回退到源码树 `../backend`，用 `python -m app.main` 启动（开发期；可用 `AIVS_PYTHON` 指定解释器）。
3. 两者都没有 → `MISSING_CAPABILITY`，附带修复建议。

运行时目录：debug 构建用仓库根 `.runtime/`（与手动跑 backend 时一致）；release 构建用
应用本地数据目录下的 `runtime/`，绝不往安装目录写文件。

## 本机状态：未验证

这台机器上 **没有 Rust 工具链**（`cargo: command not found`），所以以下文件通过审查编写，
但 **没有编译、没有运行过**。补齐工具链后再验证：

```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
```

Windows 上装 [rustup-init.exe](https://rustup.rs) 并确保存在 MSVC 生成工具与 WebView2 运行时。

## 验证步骤（待执行）

```bash
cargo install tauri-cli --version "^2"
```

```bash
cd tauri && cargo tauri dev
```

打包前还差两件事：

- **图标**：`npx @tauri-apps/cli icon <源图 1024px>` 生成 `tauri/icons/*`，否则 `tauri build` 会失败。
- **sidecar**：把后端打成单文件可执行程序，放到 `tauri/bin/aivs-backend-<target-triple>`
  （例如 `aivs-backend-x86_64-pc-windows-msvc.exe`）。M0 阶段尚未接入 PyInstaller，
  因此现在只能走开发期的 Python 回退路径。

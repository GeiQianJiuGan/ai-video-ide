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

## 退出时要按树杀，不能只 kill 一个

打包后的 sidecar 是 PyInstaller onefile，**一个文件跑起来是两个进程**：外层 bootloader
解包，内层才是真正的 Python。Windows 的 `TerminateProcess` 不牵连子进程，所以
`Supervisor::shutdown` 先 `taskkill /T /F` 按树杀、再 `child.kill()` 兜底；Unix 上先
`kill -TERM`（bootloader 会转发，让 uvicorn 走完 lifespan、把 SQLite 的 WAL 合并回主库），
等一下再兜底。只杀外层的话内层会变成孤儿，继续占着 `project.db` 与那个回环端口，
而任务管理器里看不到我们的程序——下一次启动就报「端口被占」或「数据库被锁」。

sidecar 是 **console 构建**（`sys.stderr` 被 windowed 构建掐掉的话，Python 堆栈就进不了
`runtime/backend.stderr.log`，等于静默失败）。黑框由两处按住：这里的
`CREATE_NO_WINDOW`，加上 spec 里的 `hide_console`。

`START_TIMEOUT` 是 90s 而不是 40s：解包几十 MB 再装配 FastAPI，在冷盘 + 杀软实时扫描的
机器上比源码树那条路慢得多。

## 本机状态：未验证

这台机器上 **没有 Rust 工具链**（`cargo: command not found`），所以 `src/*.rs` 与
`tauri.conf.json` 通过审查编写，但 **没有编译、没有运行过**。补齐工具链后再验证：

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

## 打包

打包所需的两样东西（图标、sidecar）都已经有脚本了，整条流水线是一条命令，详见
[docs/06-打包与分发.md](../docs/06-打包与分发.md)：

```bash
python scripts/build_desktop.py
```

先体检、缺什么说什么（不构建）：

```bash
python scripts/build_desktop.py --check
```

它按顺序跑图标（`scripts/make_icons.py`，纯 Python 画，产物已进版本库）→ 内置 FFmpeg
（`scripts/fetch_ffmpeg.py --for-tauri`）→ sidecar（`scripts/build_sidecar.py`，PyInstaller
onefile，打完会真的启动一次并建一个空工程自检）→ `cargo tauri build`。
`externalBin` 要的 `<name>-<target-triple>` 命名由脚本摆好，不用手动改名。


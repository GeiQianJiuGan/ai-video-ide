//! Python 后端 sidecar 的托管。
//!
//! 两条硬约束落在这里：
//! 1. 后端只监听 127.0.0.1 的随机端口，端口 + 一次性 token 由 `.runtime/endpoint.json` 传递，
//!    token 由本进程生成后通过环境变量注入，壳不信任文件里的 token（只用它做一致性校验）。
//! 2. 绝不静默失败——任何一步失败都返回结构化 `BootError`（code/title/detail/suggestions），
//!    由 boot-error.html 完整呈现，绝不出现白屏。

use std::env::consts::EXE_SUFFIX;
use std::fs;
use std::net::{SocketAddr, TcpStream};
#[cfg(windows)]
use std::os::windows::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};

/// 后端从冷启动到可接受连接的最长等待。
///
/// 打包后的 sidecar 是 PyInstaller 单文件产物：每次启动都要先把几十 MB 解包到临时
/// 目录，再装配 FastAPI，比源码树里那条路慢得多（冷盘 + 杀软实时扫描的机器上尤甚），
/// 所以这个预算按打包后的最坏情况给。
const START_TIMEOUT: Duration = Duration::from_secs(90);
const POLL_INTERVAL: Duration = Duration::from_millis(150);

/// `CREATE_NO_WINDOW`——不给 console 子系统的子进程分配控制台窗口。
#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

#[derive(Debug, Deserialize)]
struct EndpointFile {
    host: String,
    port: u16,
    base_url: String,
    ws_url: String,
    token: String,
    version: String,
}

/// 注入给前端 `window.__AIVS_ENDPOINT__` 的接入点。
#[derive(Debug, Clone, Serialize)]
pub struct Endpoint {
    #[serde(rename = "baseUrl")]
    pub base_url: String,
    #[serde(rename = "wsUrl")]
    pub ws_url: String,
    pub token: String,
    pub version: String,
}

/// 与后端 `{ error: { code, title, detail, suggestions } }` 同构的启动失败契约。
#[derive(Debug, Clone, Serialize)]
pub struct BootError {
    pub code: String,
    pub title: String,
    pub detail: String,
    pub suggestions: Vec<String>,
}

impl BootError {
    pub fn new(code: &str, title: &str, detail: impl Into<String>, suggestions: &[&str]) -> Self {
        Self {
            code: code.to_string(),
            title: title.to_string(),
            detail: detail.into(),
            suggestions: suggestions.iter().map(|s| s.to_string()).collect(),
        }
    }
}

/// 持有 sidecar 子进程；壳退出（含 panic）时一并杀掉，绝不留孤儿进程。
pub struct Supervisor {
    child: Option<Child>,
}

impl Supervisor {
    pub fn shutdown(&mut self) {
        if let Some(mut child) = self.child.take() {
            kill_tree(&child);
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

/// 把 sidecar **整棵进程树**收掉。
///
/// PyInstaller 的单文件产物是**两个进程**：外层 bootloader 把自己解包到临时目录，
/// 真正的 Python 是它的子进程。所以只 `child.kill()` 会留下一个孤儿——它继续占着
/// project.db 与那个回环端口，用户关掉窗口之后下一次启动就会撞上「端口被占」或
/// 「数据库被锁」，而任务管理器里看不到我们的程序。
///
/// Windows：`TerminateProcess` 不会牵连子进程，只能靠 `taskkill /T` 按树杀。
/// Unix：bootloader 会把 `SIGTERM` 转发给里层，所以先温和地要求退出（顺带让
/// uvicorn 走完 lifespan、把 SQLite 的 WAL 合并回主库），之后再由 `child.kill()` 兜底。
fn kill_tree(child: &Child) {
    let pid = child.id();
    #[cfg(windows)]
    {
        let _ = Command::new("taskkill")
            .args(["/T", "/F", "/PID", &pid.to_string()])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status();
    }
    #[cfg(not(windows))]
    {
        let _ = Command::new("kill")
            .args(["-TERM", &pid.to_string()])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status();
        // 给 uvicorn 一点收尾时间；到点了就交给外面的 child.kill()。
        std::thread::sleep(Duration::from_millis(1200));
    }
}

impl Drop for Supervisor {
    fn drop(&mut self) {
        self.shutdown();
    }
}

/// 打包后的 sidecar 与主程序同目录（Tauri externalBin 的落点）。
fn bundled_sidecar() -> Option<PathBuf> {
    let exe = std::env::current_exe().ok()?;
    let candidate = exe
        .parent()?
        .join(format!("aivs-backend{}", EXE_SUFFIX));
    candidate.is_file().then_some(candidate)
}

/// 随应用分发的二进制（ffmpeg / ffprobe）所在目录。
///
/// Tauri 的 externalBin 会把 `bin/ffmpeg-<triple>` 装成主程序旁边的 `ffmpeg`，
/// 所以这个目录就是主程序目录。开发期没有这些文件，后端自己会回退到 `<repo>/bin`
/// （见 backend/app/core/ffmpeg.py），所以这里找不到就不注入，让后端去回退。
fn bundle_dir() -> Option<PathBuf> {
    let exe = std::env::current_exe().ok()?;
    let dir = exe.parent()?.to_path_buf();
    dir.join(format!("ffmpeg{}", EXE_SUFFIX))
        .is_file()
        .then_some(dir)
}

/// 开发期回退：直接用解释器跑源码树里的 backend。
fn dev_backend_dir() -> Option<PathBuf> {
    let dir = Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()?
        .join("backend");
    dir.join("app").join("main.py").is_file().then_some(dir)
}

/// 解释器优先级：显式指定 > 项目 venv > PATH。
///
/// venv 必须排在 PATH 之前：机器上的全局 python 往往装着版本不匹配的 fastapi/starlette，
/// 用它启动会在 import 期就 TypeError，而 venv 里才是 pyproject 锁定的那套依赖。
fn resolve_python(backend_dir: &Path) -> PathBuf {
    if let Ok(explicit) = std::env::var("AIVS_PYTHON") {
        return PathBuf::from(explicit);
    }
    let venv = if cfg!(windows) {
        backend_dir.join(".venv").join("Scripts").join("python.exe")
    } else {
        backend_dir.join(".venv").join("bin").join("python")
    };
    if venv.is_file() {
        return venv;
    }
    PathBuf::from(format!("python{EXE_SUFFIX}"))
}

fn resolve_command() -> Result<Command, BootError> {
    if let Some(sidecar) = bundled_sidecar() {
        return Ok(Command::new(sidecar));
    }
    if let Some(backend_dir) = dev_backend_dir() {
        let mut cmd = Command::new(resolve_python(&backend_dir));
        cmd.args(["-m", "app.main"]).current_dir(backend_dir);
        return Ok(cmd);
    }
    Err(BootError::new(
        "MISSING_CAPABILITY",
        "找不到后端程序",
        "既没有随安装包分发的 aivs-backend sidecar，也没有源码树中的 backend/app/main.py。",
        &[
            "开发期请在仓库内运行，确保 backend/ 目录存在",
            "打包时先构建 sidecar 并放到 tauri/bin/aivs-backend-<target-triple>",
        ],
    ))
}

/// 启动后端并等到它真正可连接。成功返回进程守卫与接入点。
pub fn launch(runtime_dir: &Path) -> Result<(Supervisor, Endpoint), BootError> {
    fs::create_dir_all(runtime_dir).map_err(|e| {
        BootError::new(
            "INTERNAL",
            "无法创建运行时目录",
            format!("{}：{e}", runtime_dir.display()),
            &["检查该路径的写权限，或磁盘是否已满"],
        )
    })?;

    let endpoint_path = runtime_dir.join("endpoint.json");
    // 必须先删掉上一次的残留，否则会把旧端口当成本次启动结果。
    let _ = fs::remove_file(&endpoint_path);

    let token = uuid::Uuid::new_v4().to_string();
    let stderr_path = runtime_dir.join("backend.stderr.log");
    let stderr_sink = fs::File::create(&stderr_path).map_err(|e| {
        BootError::new(
            "INTERNAL",
            "无法写入后端日志",
            format!("{}：{e}", stderr_path.display()),
            &["检查运行时目录的写权限"],
        )
    })?;

    let mut cmd = resolve_command()?;
    cmd.env("AIVS_HOST", "127.0.0.1")
        .env("AIVS_PORT", "0")
        .env("AIVS_REQUIRE_HANDSHAKE", "true")
        .env("AIVS_HANDSHAKE_TOKEN", &token)
        .env("AIVS_RUNTIME_DIR", runtime_dir)
        .env("PYTHONUTF8", "1")
        .env("PYTHONIOENCODING", "utf-8")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::from(stderr_sink));
    // 内置 FFmpeg：告诉后端去哪找，用户不必自己装（找不到时不注入，后端回退到 <repo>/bin）。
    if let Some(dir) = bundle_dir() {
        cmd.env("AIVS_BUNDLE_DIR", dir);
    }
    // sidecar 是 console 子系统的程序（必须如此，否则 Python 的堆栈进不了 stderr 日志），
    // 而壳是 windows 子系统——不加这个标志，每次启动都会在屏幕上闪一个黑框。
    #[cfg(windows)]
    cmd.creation_flags(CREATE_NO_WINDOW);

    let mut child = cmd.spawn().map_err(|e| {
        BootError::new(
            "MISSING_CAPABILITY",
            "无法启动后端进程",
            format!("{e}"),
            &[
                "确认 Python 3.11+ 已安装并在 PATH 中，或用 AIVS_PYTHON 指定解释器",
                "在 backend/ 目录执行依赖安装后重试",
            ],
        )
    })?;

    let deadline = Instant::now() + START_TIMEOUT;
    loop {
        // 子进程先退出说明后端自己崩了，日志里有真实原因，不能干等到超时。
        if let Ok(Some(status)) = child.try_wait() {
            return Err(BootError::new(
                "INTERNAL",
                "后端进程启动后立即退出",
                format!(
                    "退出码 {}。详细堆栈见 {}",
                    status.code().unwrap_or(-1),
                    stderr_path.display()
                ),
                &[
                    "打开上面的日志文件查看 Python 异常",
                    "确认 backend 依赖已安装（pip install -e backend）",
                ],
            ));
        }

        if let Some(endpoint) = read_ready_endpoint(&endpoint_path, &token) {
            return Ok((Supervisor { child: Some(child) }, endpoint));
        }

        if Instant::now() >= deadline {
            kill_tree(&child);
            let _ = child.kill();
            let _ = child.wait();
            return Err(BootError::new(
                "INTERNAL",
                "后端启动超时",
                format!(
                    "{} 秒内没有等到可用的 {}。日志见 {}",
                    START_TIMEOUT.as_secs(),
                    endpoint_path.display(),
                    stderr_path.display()
                ),
                &[
                    "查看后端日志定位卡住的位置",
                    "确认 127.0.0.1 的回环网络没有被安全软件拦截",
                ],
            ));
        }
        std::thread::sleep(POLL_INTERVAL);
    }
}

/// endpoint.json 写在 uvicorn 起监听之前，所以文件存在不代表能连；必须再做一次 TCP 探活。
fn read_ready_endpoint(path: &Path, expected_token: &str) -> Option<Endpoint> {
    let raw = fs::read_to_string(path).ok()?;
    let parsed: EndpointFile = serde_json::from_str(&raw).ok()?;
    if parsed.token != expected_token {
        return None; // 还是上一次的残留文件
    }
    let addr: SocketAddr = format!("{}:{}", parsed.host, parsed.port).parse().ok()?;
    TcpStream::connect_timeout(&addr, Duration::from_millis(300)).ok()?;
    Some(Endpoint {
        base_url: parsed.base_url,
        ws_url: parsed.ws_url,
        token: parsed.token,
        version: parsed.version,
    })
}

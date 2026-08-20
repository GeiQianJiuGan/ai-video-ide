// 发布版不弹控制台窗口；调试版保留，方便看 Rust 侧日志。
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod backend;

use std::path::PathBuf;
use std::sync::Mutex;

use tauri::{Manager, RunEvent, WebviewUrl, WebviewWindowBuilder};

use crate::backend::{BootError, Endpoint, Supervisor};

/// 开发期用仓库里的 .runtime，和直接跑 backend / Vite 时看到的是同一份；
/// 打包后落在用户数据目录，避免往安装目录写文件。
fn resolve_runtime_dir(app: &tauri::AppHandle) -> Result<PathBuf, BootError> {
    if cfg!(debug_assertions) {
        if let Some(repo_root) = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).parent() {
            return Ok(repo_root.join(".runtime"));
        }
    }
    app.path().app_local_data_dir().map(|d| d.join("runtime")).map_err(|e| {
        BootError::new(
            "INTERNAL",
            "无法定位应用数据目录",
            format!("{e}"),
            &["检查当前用户的 AppData / XDG 数据目录是否可写"],
        )
    })
}

fn open_workbench(app: &tauri::AppHandle, endpoint: &Endpoint) -> tauri::Result<()> {
    // 端口与 token 只在窗口创建时注入一次，前端不需要、也拿不到别的通道去猜。
    let payload = serde_json::to_string(endpoint).expect("Endpoint 序列化不会失败");
    let script = format!("window.__AIVS_ENDPOINT__ = {payload};");

    WebviewWindowBuilder::new(app, "main", WebviewUrl::default())
        .title("AI Video Studio")
        .inner_size(1600.0, 980.0)
        .min_inner_size(1180.0, 720.0)
        .initialization_script(script.as_str())
        .build()?;
    Ok(())
}

fn open_boot_error(app: &tauri::AppHandle, err: &BootError) -> tauri::Result<()> {
    let payload = serde_json::to_string(err).expect("BootError 序列化不会失败");
    let script = format!("window.__AIVS_BOOT_ERROR__ = {payload};");

    WebviewWindowBuilder::new(app, "boot-error", WebviewUrl::App("boot-error.html".into()))
        .title("AI Video Studio — 启动失败")
        .inner_size(760.0, 520.0)
        .resizable(true)
        .initialization_script(script.as_str())
        .build()?;
    Ok(())
}

fn main() {
    let app = tauri::Builder::default()
        .setup(|app| {
            let handle = app.handle().clone();
            let boot = resolve_runtime_dir(&handle).and_then(|dir| backend::launch(&dir));
            match boot {
                Ok((supervisor, endpoint)) => {
                    app.manage(Mutex::new(supervisor));
                    open_workbench(&handle, &endpoint)?;
                }
                // 绝不白屏：把失败原因和修复建议直接摆在用户面前。
                Err(err) => {
                    eprintln!("[aivs] backend boot failed: {} — {}", err.title, err.detail);
                    open_boot_error(&handle, &err)?;
                }
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("Tauri 应用装配失败");

    app.run(|handle, event| {
        if let RunEvent::Exit = event {
            // 显式收尾，不依赖 Drop 的时机，确保 sidecar 不变成孤儿进程。
            if let Some(state) = handle.try_state::<Mutex<Supervisor>>() {
                if let Ok(mut guard) = state.lock() {
                    guard.shutdown();
                }
            }
        }
    });
}

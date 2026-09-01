"""把后端打成单文件 sidecar，摆进 `tauri/bin/aivs-backend-<target-triple>`。

用法（在仓库根目录）：

    python scripts/build_sidecar.py              # 打包并按 target triple 就位
    python scripts/build_sidecar.py --triple x86_64-pc-windows-msvc   # 没装 rustc 时手写
    python scripts/build_sidecar.py --skip-verify                     # 跳过启动自检

为什么要有这一步：Tauri 的 externalBin 只认**单个文件**，而后端是一棵 Python
源码树。PyInstaller 把它连解释器一起收成一个可执行文件，Tauri 打包时再把
`-<triple>` 后缀去掉，装成主程序旁边的 `aivs-backend[.exe]`——正好是
`tauri/src/backend.rs::bundled_sidecar()` 第一个去找的位置。

做完要能验证（这是本脚本存在的另一半理由）：打出来的东西会被真的启动一次，
等它写出 `endpoint.json` 并连上那个端口，再关掉。「打包成功但跑不起来」
是这条链上最贵的一种失败——它要到用户装完双击才暴露。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import NoReturn

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND = REPO_ROOT / "backend"
SPEC = BACKEND / "aivs-backend.spec"
TAURI_BIN = REPO_ROOT / "tauri" / "bin"
EXE_SUFFIX = ".exe" if os.name == "nt" else ""
#: 冷启动预算。首次跑要解包几十 MB 再装配 FastAPI，比裸解释器慢得多。
START_TIMEOUT = 90.0


def die(title: str, *suggestions: str) -> NoReturn:
    """照后端那套四要素报错：说清是什么、然后给能照着做的下一步。"""
    lines = [f"打包中止：{title}"]
    lines += [f"  · {s}" for s in suggestions]
    raise SystemExit("\n".join(lines))


def venv_python() -> Path | None:
    """backend/.venv 里那个解释器。

    必须优先于当前解释器：全局 python 上装的 fastapi / starlette 往往版本不匹配，
    用它打包等于把一套跑不起来的依赖冻进安装包。
    """
    candidate = (
        BACKEND / ".venv" / ("Scripts" if os.name == "nt" else "bin") / f"python{EXE_SUFFIX}"
    )
    return candidate if candidate.is_file() else None


def resolve_python(explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            die(f"指定的解释器不存在：{path}", "检查 --python 的路径")
        return path
    return venv_python() or Path(sys.executable)


def ensure_pyinstaller(python: Path) -> str:
    proc = subprocess.run(  # noqa: S603
        [str(python), "-c", "import PyInstaller;print(PyInstaller.__version__)"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        die(
            f"{python} 里没有 PyInstaller",
            f'安装："{python}" -m pip install "pyinstaller>=6.11"',
            '或连开发依赖一起装：cd backend && python -m pip install -e ".[package]"',
        )
    return proc.stdout.strip()


def host_triple(explicit: str | None) -> str:
    """Tauri 的 externalBin 要求文件名带 target triple。"""
    if explicit:
        return explicit
    try:
        proc = subprocess.run(  # noqa: S603
            ["rustc", "-vV"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        proc = None
    if proc and proc.returncode == 0:
        for line in proc.stdout.splitlines():
            if line.startswith("host:"):
                return line.split(":", 1)[1].strip()
    die(
        "拿不到 target triple（没有 rustc）",
        "安装 Rust 工具链：https://rustup.rs —— Tauri 打包本来就需要它",
        "或用 --triple 手写，例如 x86_64-pc-windows-msvc / x86_64-unknown-linux-gnu",
    )


def build(python: Path, clean: bool) -> Path:
    if not SPEC.is_file():
        die(f"找不到 spec：{SPEC}", "确认仓库完整，backend/aivs-backend.spec 应该在版本库里")
    cmd = [str(python), "-m", "PyInstaller", str(SPEC), "--noconfirm", "--distpath", "dist"]
    if clean:
        cmd.append("--clean")
    print(f"打包后端：{' '.join(cmd)}")
    # cwd 必须是 backend/：spec 里的相对路径与 pathex 都按它算。
    proc = subprocess.run(cmd, cwd=BACKEND)  # noqa: S603
    if proc.returncode != 0:
        die(
            f"PyInstaller 退出码 {proc.returncode}",
            "往上翻它的输出，通常是漏了一个隐式导入——补进 backend/aivs-backend.spec 的 hiddenimports",
        )
    built = BACKEND / "dist" / f"aivs-backend{EXE_SUFFIX}"
    if not built.is_file():
        die(f"打包结束但没有产物：{built}", "确认 spec 里 EXE(name=...) 仍然是 aivs-backend")
    return built


def verify(exe: Path) -> None:
    """真的启动一次：等 endpoint.json → 连那个端口 → 打一次 /health → 关掉。"""
    print("自检：启动一次打好的后端…")
    # ignore_cleanup_errors：自检是 terminate 掉进程的，Windows 上 SQLite 的句柄
    # 可能比进程退出晚一步释放，删不掉临时目录不该让「后端能跑」这个结论翻车。
    with tempfile.TemporaryDirectory(
        prefix="aivs-sidecar-check-", ignore_cleanup_errors=True
    ) as tmp:
        runtime = Path(tmp)
        env = {
            **os.environ,
            "AIVS_HOST": "127.0.0.1",
            "AIVS_PORT": "0",
            "AIVS_RUNTIME_DIR": str(runtime),
            "AIVS_REQUIRE_HANDSHAKE": "false",
            "PYTHONUTF8": "1",
        }
        log_path = runtime / "stderr.log"
        with log_path.open("wb") as sink:
            proc = subprocess.Popen(  # noqa: S603
                [str(exe)], env=env, stdout=subprocess.DEVNULL, stderr=sink
            )
            try:
                endpoint = _await_endpoint(proc, runtime / "endpoint.json", log_path)
                _probe_health(endpoint)
                _probe_new_project(endpoint, runtime / "probe-project", log_path)
            finally:
                _kill_tree(proc)
    print("自检通过：后端能起、能连、能建工程（迁移脚本确实打进去了）。")


def _kill_tree(proc: subprocess.Popen) -> None:
    """把 sidecar **整棵树**收掉。

    PyInstaller 的单文件产物是两个进程：外层 bootloader 解包，真正的 Python 是它的
    子进程。Windows 上 `TerminateProcess` 只杀外层，里层会变成孤儿——继续占着
    project.db 与那个端口，下一次打包连 dist/ 里的 exe 都删不掉。
    Unix 上 bootloader 会转发 SIGTERM，所以先温和地要求退出，再兜底强杀。
    壳那边同一个问题、同一套解法，见 tauri/src/backend.rs::Supervisor::shutdown。
    """
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(  # noqa: S603
            ["taskkill", "/T", "/F", "/PID", str(proc.pid)],  # noqa: S607
            capture_output=True,
        )
    else:
        proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()


def _await_endpoint(proc: subprocess.Popen, path: Path, log_path: Path) -> dict:
    deadline = time.monotonic() + START_TIMEOUT
    while True:
        if (code := proc.poll()) is not None:
            die(
                f"打好的后端启动后立即退出（退出码 {code}）",
                f"日志：{_tail(log_path)}",
                "多半是漏了隐式导入或数据文件，补进 backend/aivs-backend.spec",
            )
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = None
            if data and _connectable(data["host"], int(data["port"])):
                return data
        if time.monotonic() >= deadline:
            _kill_tree(proc)
            die(
                f"{START_TIMEOUT:.0f} 秒内没等到后端可连接",
                f"日志：{_tail(log_path)}",
                "确认 127.0.0.1 回环没被安全软件拦；也可以先 --skip-verify 打出来再手工排查",
            )
        time.sleep(0.3)


def _connectable(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def _probe_health(endpoint: dict) -> None:
    url = f"{endpoint['base_url']}/health"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        die(f"打好的后端连上了但 {url} 不正常：{exc}", "手工跑一次那个可执行文件看它的日志")
    print(f"  /health → {body}")


def _probe_new_project(endpoint: dict, target: Path, log_path: Path) -> None:
    """建一个空工程。

    这一步才真正跑 alembic：`/health` 一行迁移都不碰，所以「迁移脚本没打进去」
    这种失败只有建库时才暴露——而那已经是用户装完双击之后了。
    """
    url = f"{endpoint['base_url']}/projects"
    payload = json.dumps({"dir": str(target), "name": "打包自检"}).encode("utf-8")
    req = urllib.request.Request(  # noqa: S310
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:600]
        die(
            f"建工程失败（HTTP {exc.code}）：{detail}",
            "多半是 alembic.ini / alembic/versions 没进 bundle："
            "对一下 backend/aivs-backend.spec 的 datas 与 migrate.py::_backend_root()",
            f"日志：{_tail(log_path)}",
        )
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        die(f"建工程时连不上：{exc}", f"日志：{_tail(log_path)}")
    if not (target / "project.db").is_file():
        die("接口回了成功但目录里没有 project.db", f"看一下 {target}")
    print(f"  /projects → schema_version={body.get('schema_version')} · {target.name}/project.db")


def _tail(path: Path, limit: int = 1200) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return f"{path}（读不出来）"
    return f"{path}\n{text[-limit:]}" if text else f"{path}（是空的）"


def stage(built: Path, triple: str) -> Path:
    TAURI_BIN.mkdir(parents=True, exist_ok=True)
    target = TAURI_BIN / f"aivs-backend-{triple}{EXE_SUFFIX}"
    shutil.copy2(built, target)
    if os.name != "nt":
        target.chmod(0o755)
    size = target.stat().st_size / 1e6
    print(f"就位：{target.relative_to(REPO_ROOT)} · {size:.1f} MB")
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="把后端打成 Tauri sidecar")
    parser.add_argument("--python", help="用哪个解释器打包；默认 backend/.venv 里那个")
    parser.add_argument("--triple", help="target triple；默认问 rustc")
    parser.add_argument("--clean", action="store_true", help="先清掉 PyInstaller 缓存再打")
    parser.add_argument("--skip-verify", action="store_true", help="不启动自检（不推荐）")
    args = parser.parse_args()

    python = resolve_python(args.python)
    print(f"解释器：{python} · PyInstaller {ensure_pyinstaller(python)}")
    triple = host_triple(args.triple)
    print(f"target triple：{triple}")

    built = build(python, args.clean)
    if not args.skip_verify:
        verify(built)
    stage(built, triple)
    print("\n完成。接着可以跑 python scripts/build_desktop.py 出安装包。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

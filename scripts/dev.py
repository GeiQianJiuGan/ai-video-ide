"""一键起开发环境：后端（固定端口）+ 前端 Vite dev server。

用法（仓库根目录）：

    python scripts/dev.py                  # 后端 + 前端，起好后自动开浏览器
    python scripts/dev.py --port 8899      # 换后端端口（Vite 代理跟着改）
    python scripts/dev.py --backend-only   # 只起后端
    python scripts/dev.py --no-open        # 不开浏览器

也可以直接跑仓库根的 start.cmd（Windows）/ start.sh（macOS、Linux），
它们只是找一个 Python 再转调本脚本。

为什么要有它：开发期没有 Tauri 壳，本来得开两个终端，还得记住「后端必须落在
8765，否则 Vite 代理打不中」（见 frontend/vite.config.ts）。这里把两条命令、
端口约定与依赖体检收成一条，并且照「绝不静默失败」的规矩来——任一前置条件不满足
就报出能照着做的下一步，而不是起一个半死的环境。
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import NoReturn

IS_WIN = os.name == "nt"
REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
FRONTEND_DIR = REPO_ROOT / "frontend"
#: 后端默认端口，必须与 frontend/vite.config.ts 里代理的默认目标一致。
DEFAULT_PORT = 8765
#: 前端端口。vite.config.ts 用了 strictPort：被占用时 Vite 直接失败而不是换一个。
WEB_PORT = 5173


def _tolerate_console_encoding() -> None:
    """让本终端编码不下的字符退化成占位符，而不是把转发线程炸掉。

    Windows 控制台默认是 GBK，而 Vite 会打印 `➜`：`print` 直接抛
    UnicodeEncodeError，转发线程死掉之后前端日志就再也不出现了——环境看着是活的，
    实际瞎了一半。这属于「绝不静默失败」要防的那类事故，所以在最外层一次性关掉。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(errors="replace")
        except (ValueError, OSError):
            pass


def say(msg: str) -> None:
    print(f"[dev] {msg}", flush=True)


def die(title: str, *suggestions: str) -> NoReturn:
    """四要素错误的脚本版：说清哪里不对 + 下一步照着敲什么。"""
    raise SystemExit("\n".join([f"[dev] {title}", *(f"  · {s}" for s in suggestions if s)]))


def backend_python() -> str:
    """优先用 backend/.venv 里那个解释器——CLAUDE.md 里所有后端命令都是它。"""
    sub = "Scripts" if IS_WIN else "bin"
    exe = "python.exe" if IS_WIN else "python"
    candidate = BACKEND_DIR / ".venv" / sub / exe
    if candidate.is_file():
        return str(candidate)
    say(f"没有 backend/.venv，退回当前解释器：{sys.executable}")
    return sys.executable


def check_backend(py: str) -> None:
    if not (BACKEND_DIR / "app" / "main.py").is_file():
        die("找不到 backend/app/main.py", "在仓库根目录运行这个脚本")
    probe = subprocess.run(  # noqa: S603
        [py, "-c", "import fastapi, uvicorn, pydantic_settings"],
        cwd=str(BACKEND_DIR),
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        tail = (probe.stderr or "").strip().splitlines()
        die(
            "后端依赖没装齐（import fastapi / uvicorn 失败）",
            'cd backend && python -m pip install -e ".[dev]"',
            tail[-1] if tail else "",
        )


def check_frontend() -> str:
    npm = shutil.which("npm")
    if not npm:
        die(
            "PATH 里没有 npm",
            "装 Node.js 20+（https://nodejs.org）",
            "只想起后端的话：python scripts/dev.py --backend-only",
        )
    if not (FRONTEND_DIR / "node_modules").is_dir():
        die("frontend/node_modules 不存在", "cd frontend && npm install")
    return npm


def check_ffmpeg() -> None:
    """缺 FFmpeg 只提醒，不拦启动——抽帧 / 转码 / 导出会报 FFMPEG_MISSING，别的路径照常。"""
    suffix = ".exe" if IS_WIN else ""
    if all((REPO_ROOT / "bin" / f"{t}{suffix}").is_file() for t in ("ffmpeg", "ffprobe")):
        return
    fallback = "会用 PATH 里那份" if shutil.which("ffmpeg") else "PATH 里也没有"
    say(f"提醒：bin/ 里没有内置 FFmpeg（{fallback}）→ python scripts/fetch_ffmpeg.py")


def port_taken(port: int, host: str = "127.0.0.1") -> bool:
    """能连上就算被占用。

    探前端必须用 host="localhost"：Vite 默认只听 localhost，而 Windows 上它先解析成
    ::1——硬写 127.0.0.1 会得出「没起来」的错结论。create_connection 会把 v4 / v6 都试。
    """
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


class Child:
    """一个子进程 + 一个把它输出带前缀转发到本终端的线程。"""

    def __init__(self, name: str, cmd: list[str], cwd: Path, env: dict[str, str]) -> None:
        self.name = name
        self.proc = subprocess.Popen(  # noqa: S603
            cmd,
            cwd=str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            # Windows：单独的进程组，Ctrl+C 只进本脚本，由 stop() 统一收摊；
            # POSIX：单独的会话，方便按进程组整棵树杀。
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if IS_WIN else 0,
            start_new_session=not IS_WIN,
        )
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        stream = self.proc.stdout
        if stream is None:
            return
        for line in stream:
            print(f"[{self.name}] {line.rstrip()}", flush=True)

    @property
    def alive(self) -> bool:
        return self.proc.poll() is None

    def stop(self) -> None:
        if not self.alive:
            return
        # Windows 上 npm.cmd 会再拉起 node，只杀父进程会留下孤儿 vite 占着 5173，
        # 所以整棵树一起杀；POSIX 侧杀进程组是同一个意思。
        try:
            if IS_WIN:
                subprocess.run(  # noqa: S603
                    ["taskkill", "/F", "/T", "/PID", str(self.proc.pid)],  # noqa: S607
                    capture_output=True,
                    check=False,
                )
            else:
                os.killpg(self.proc.pid, signal.SIGTERM)
        except OSError:
            self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def stop_all(children: list[Child]) -> None:
    for child in reversed(children):
        child.stop()


def wait_ready(child: Child, url: str, timeout: float) -> bool:
    """等到 url 真的应答；子进程中途死了立刻返回，别让人干等满整个 timeout。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not child.alive:
            return False
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:  # noqa: S310
                resp.read()  # 读完再关，不然服务端日志里会多出一串 ConnectionReset
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.4)
    return False


def main() -> int:
    _tolerate_console_encoding()
    parser = argparse.ArgumentParser(description="一键起开发环境（后端 + 前端）")
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("AIVS_PORT") or DEFAULT_PORT),
        help=f"后端端口，默认 {DEFAULT_PORT}（Vite 代理的默认目标）",
    )
    parser.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--backend-only", action="store_true", help="只起后端")
    parser.add_argument(
        "--frontend-only",
        action="store_true",
        help="只起前端；后端得另外自己起，否则界面一直连不上",
    )
    args = parser.parse_args()
    if args.backend_only and args.frontend_only:
        die("--backend-only 与 --frontend-only 不能一起给")

    want_backend = not args.frontend_only
    want_frontend = not args.backend_only
    base_url = f"http://127.0.0.1:{args.port}"
    py = npm = ""

    # 先把所有体检做完再拉进程：宁可一个都没起，也不要起一半留个半死的环境。
    if want_backend:
        py = backend_python()
        check_backend(py)
        if port_taken(args.port):
            die(
                f"端口 {args.port} 已被占用",
                "后端可能已经在跑了：直接开 http://localhost:5173，或加 --frontend-only",
                f"也可以换端口：python scripts/dev.py --port {args.port + 1}",
            )
    if want_frontend:
        npm = check_frontend()
        if port_taken(WEB_PORT, "localhost"):
            die(
                f"端口 {WEB_PORT} 已被占用（vite.config.ts 是 strictPort，Vite 不会自己换）",
                "停掉占用它的进程（很可能是上一次没退干净的 vite）",
                "或者加 --backend-only 只起后端",
            )
    check_ffmpeg()

    children: list[Child] = []
    if want_backend:
        env = os.environ.copy()
        env["AIVS_PORT"] = str(args.port)
        env["PYTHONUNBUFFERED"] = "1"  # 否则日志攒在管道里，看着像卡住
        say(f"起后端 {base_url}")
        backend = Child("后端", [py, "-m", "app.main"], BACKEND_DIR, env)
        children.append(backend)
        if not wait_ready(backend, f"{base_url}/api/v1/health", 40.0):
            stop_all(children)
            die(
                "后端 40 秒内没起来（原因在上面 [后端] 的输出里）",
                'cd backend && python -m pip install -e ".[dev]"',
                f"手工跑一次看看：cd backend && AIVS_PORT={args.port} python -m app.main",
            )
        say(f"后端就绪 · {base_url}/api/v1/health")

    if want_frontend:
        env = os.environ.copy()
        env["AIVS_BACKEND"] = base_url  # vite.config.ts 的代理目标
        env.setdefault("FORCE_COLOR", "1")
        say(f"起前端 http://localhost:{WEB_PORT}")
        frontend = Child("前端", [npm, "run", "dev"], FRONTEND_DIR, env)
        children.append(frontend)
        if not wait_ready(frontend, f"http://localhost:{WEB_PORT}/", 120.0):
            stop_all(children)
            die(
                "前端 dev server 没起来（原因在上面 [前端] 的输出里）",
                "cd frontend && npm install",
                "cd frontend && npm run dev",
            )
        if not args.no_open:
            webbrowser.open(f"http://localhost:{WEB_PORT}")

    say("都起来了。Ctrl+C 一起停。")
    code = 0
    try:
        while True:
            dead = next((c for c in children if not c.alive), None)
            if dead is not None:
                code = dead.proc.returncode or 1
                say(f"{dead.name}退出（退出码 {code}）——把另一半也停掉，不留半死的环境")
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        print()
        say("收到 Ctrl+C，停止…")
    finally:
        stop_all(children)
    return code


if __name__ == "__main__":
    sys.exit(main())

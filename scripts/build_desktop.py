"""一条命令出安装包：Windows 的 .exe 安装器 / Linux 的 AppImage · deb · rpm。

用法（在仓库根目录）：

    python scripts/build_desktop.py                    # 按当前平台出默认格式
    python scripts/build_desktop.py --targets nsis     # 只出 Windows 安装器
    python scripts/build_desktop.py --targets appimage,deb
    python scripts/build_desktop.py --check            # 只体检，不构建
    python scripts/build_desktop.py --skip-ffmpeg      # bin/ 里已经有了，别再下 150MB

**不能交叉编译**：Tauri 的壳是原生程序，Windows 包只能在 Windows 上出，Linux 包只能
在 Linux 上出。想一次拿到两个平台的产物走 CI（`.github/workflows/release.yml` 就是
这份脚本在两台机器上各跑一次）。

顺序是固定的，每一步都得在下一步开始前就位，否则 `cargo tauri build` 会在编译完
十分钟之后才因为「少一张图标」失败：

  1. 图标      scripts/make_icons.py     —— bundle.icon 列的文件必须存在
  2. FFmpeg    scripts/fetch_ffmpeg.py   —— externalBin 的 bin/ffmpeg-<triple>
  3. sidecar   scripts/build_sidecar.py  —— externalBin 的 bin/aivs-backend-<triple>
  4. 前端      由 tauri.conf.json 的 beforeBuildCommand 触发（npm run build）
  5. 打包      cargo tauri build --bundles ...
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
TAURI = REPO_ROOT / "tauri"
FRONTEND = REPO_ROOT / "frontend"
BUNDLE_DIR = TAURI / "target" / "release" / "bundle"

#: 每个平台默认出哪些格式。dmg 只有 macOS 认，nsis 只有 Windows 认——
#: 混在一起交给 tauri 只会得到一串「skipping」。
DEFAULT_TARGETS: dict[str, tuple[str, ...]] = {
    "windows": ("nsis",),
    "linux": ("appimage", "deb", "rpm"),
    "darwin": ("dmg",),
}
#: 产物落在 bundle/<子目录>/ 下，用来在结尾把文件真的列出来（而不是让用户自己找）。
BUNDLE_SUBDIR: dict[str, str] = {
    "nsis": "nsis",
    "msi": "msi",
    "appimage": "appimage",
    "deb": "deb",
    "rpm": "rpm",
    "dmg": "dmg",
    "app": "macos",
}


def die(title: str, *suggestions: str) -> NoReturn:
    lines = [f"打包中止：{title}"]
    lines += [f"  · {s}" for s in suggestions]
    raise SystemExit("\n".join(lines))


def platform_key() -> str:
    system = platform.system().lower()
    if system.startswith("win"):
        return "windows"
    return "darwin" if system == "darwin" else "linux"


def step(index: int, total: int, title: str) -> None:
    print(f"\n=== [{index}/{total}] {title} " + "=" * max(0, 46 - len(title)))


def run(cmd: list[str], cwd: Path, what: str, *suggestions: str) -> None:
    print(f"$ {' '.join(cmd)}")
    proc = subprocess.run(cmd, cwd=cwd)  # noqa: S603
    if proc.returncode != 0:
        die(f"{what} 失败（退出码 {proc.returncode}）", *suggestions)


def python_exe() -> str:
    """跑子脚本用的解释器。

    优先 backend/.venv：`build_sidecar.py` 要在那套依赖里跑 PyInstaller，
    而 make_icons / fetch_ffmpeg 只用标准库，谁跑都一样。
    """
    venv = (
        REPO_ROOT
        / "backend"
        / ".venv"
        / ("Scripts" if os.name == "nt" else "bin")
        / ("python.exe" if os.name == "nt" else "python")
    )
    return str(venv) if venv.is_file() else sys.executable


def which(name: str) -> str | None:
    return shutil.which(name)


def doctor(key: str) -> list[str]:
    """体检。返回缺失项的说明，空列表才算能构建。"""
    problems: list[str] = []
    print("环境体检：")
    for tool, hint in (
        ("cargo", "Rust 工具链：https://rustup.rs"),
        ("rustc", "Rust 工具链：https://rustup.rs"),
        ("node", "Node 18+：https://nodejs.org"),
        ("npm", "随 Node 一起装"),
    ):
        found = which(tool)
        print(f"  {tool:<14} {found or '缺失'}")
        if not found:
            problems.append(f"缺少 {tool} —— {hint}")

    tauri_cli = which("cargo-tauri")
    print(f"  {'cargo-tauri':<14} {tauri_cli or '缺失'}")
    if not tauri_cli:
        problems.append('缺少 tauri-cli —— cargo install tauri-cli --version "^2"')

    if not (FRONTEND / "node_modules").is_dir():
        problems.append("frontend/node_modules 不在 —— cd frontend && npm install")

    if key == "linux":
        # Tauri 在 Linux 上链接 WebKitGTK；缺了它 cargo 会在链接期才报一串 pkg-config 错误。
        if not which("pkg-config"):
            problems.append("缺少 pkg-config —— 见下面那条 apt 命令")
        else:
            probe = subprocess.run(  # noqa: S603
                ["pkg-config", "--exists", "webkit2gtk-4.1"],  # noqa: S607
                capture_output=True,
            )
            print(f"  {'webkit2gtk-4.1':<14} {'ok' if probe.returncode == 0 else '缺失'}")
            if probe.returncode != 0:
                problems.append(
                    "缺少 WebKitGTK 开发包 —— sudo apt install libwebkit2gtk-4.1-dev "
                    "build-essential curl wget file libxdo-dev libssl-dev "
                    "libayatana-appindicator3-dev librsvg2-dev"
                )
        if not which("rpmbuild"):
            print("  rpmbuild       缺失（只影响 rpm，AppImage / deb 不受影响）")
    if key == "windows":
        print("  WebView2       运行期依赖，Win11 自带；Win10 由安装器按需引导")
    return problems


def resolve_targets(raw: str | None, key: str) -> list[str]:
    if not raw:
        return list(DEFAULT_TARGETS[key])
    wanted = [t.strip().lower() for t in raw.split(",") if t.strip()]
    unknown = [t for t in wanted if t not in BUNDLE_SUBDIR]
    if unknown:
        die(
            f"不认识的打包格式：{', '.join(unknown)}",
            f"可选：{', '.join(sorted(BUNDLE_SUBDIR))}",
        )
    foreign = [t for t in wanted if t not in DEFAULT_TARGETS[key] and t not in ("msi", "app")]
    if foreign:
        print(
            f"注意：{', '.join(foreign)} 不是当前平台（{key}）的格式，"
            "Tauri 会直接跳过——原生壳没法交叉编译。"
        )
    return wanted


def list_artifacts(targets: list[str]) -> list[Path]:
    out: list[Path] = []
    for target in targets:
        directory = BUNDLE_DIR / BUNDLE_SUBDIR[target]
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            # deb/rpm 目录里还有解包出来的中间目录，只报文件。
            if path.is_file():
                out.append(path)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="出桌面安装包（Windows / Linux / macOS）")
    parser.add_argument("--targets", help="逗号分隔；默认按平台选（Windows=nsis，Linux=appimage,deb,rpm）")
    parser.add_argument("--check", action="store_true", help="只体检，不构建")
    parser.add_argument("--skip-icons", action="store_true", help="跳过图标生成")
    parser.add_argument("--skip-ffmpeg", action="store_true", help="跳过 FFmpeg 下载与就位")
    parser.add_argument("--skip-sidecar", action="store_true", help="跳过后端 sidecar 打包")
    parser.add_argument(
        "--skip-sidecar-verify",
        action="store_true",
        help="打 sidecar 但不启动自检（不推荐：那一步才验证迁移脚本真的打进去了）",
    )
    parser.add_argument("--triple", help="target triple；默认问 rustc")
    args = parser.parse_args()

    key = platform_key()
    targets = resolve_targets(args.targets, key)
    print(f"平台：{key} · 打包格式：{', '.join(targets)}")

    problems = doctor(key)
    if args.check:
        if problems:
            print("\n还差这些：")
            for p in problems:
                print(f"  · {p}")
            return 1
        print("\n体检通过，可以 python scripts/build_desktop.py 出包。")
        return 0
    if problems:
        die("环境不完整", *problems)

    py = python_exe()
    total = 4
    if not args.skip_icons:
        step(1, total, "图标")
        run([py, str(SCRIPTS / "make_icons.py")], REPO_ROOT, "生成图标")

    if not args.skip_ffmpeg:
        step(2, total, "内置 FFmpeg")
        run(
            [py, str(SCRIPTS / "fetch_ffmpeg.py"), "--for-tauri"],
            REPO_ROOT,
            "准备 FFmpeg",
            "网络不通时用 --url 指向内网镜像或本地压缩包，见 scripts/fetch_ffmpeg.py",
        )

    if not args.skip_sidecar:
        step(3, total, "后端 sidecar")
        cmd = [py, str(SCRIPTS / "build_sidecar.py")]
        if args.triple:
            cmd += ["--triple", args.triple]
        if args.skip_sidecar_verify:
            cmd.append("--skip-verify")
        run(cmd, REPO_ROOT, "打包后端 sidecar")

    step(4, total, "Tauri 打包")
    run(
        ["cargo", "tauri", "build", "--bundles", ",".join(targets)],
        TAURI,
        "cargo tauri build",
        "前端报错就先单独跑 cd frontend && npm run build 看具体是哪一行",
        "Rust 侧报错时把 tauri/target 删掉重来（增量缓存偶尔会坏）",
    )

    artifacts = list_artifacts(targets)
    print("\n产物：")
    if not artifacts:
        print(f"  （{BUNDLE_DIR} 下没找到文件——上面那步可能把格式都跳过了）")
        return 1
    for path in artifacts:
        print(f"  {path.relative_to(REPO_ROOT)} · {path.stat().st_size / 1e6:.1f} MB")
    print(
        "\n装完双击就能用：Python、FFmpeg 都在包里，ComfyUI / LLM 没配也能走完手动流程。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

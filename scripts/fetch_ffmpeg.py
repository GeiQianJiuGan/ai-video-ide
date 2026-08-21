"""把 FFmpeg / FFprobe 下载进 `<repo>/bin`，让应用自带一份。

用法（在仓库根目录）：

    python scripts/fetch_ffmpeg.py             # 按当前平台下载
    python scripts/fetch_ffmpeg.py --force     # 已经有了也重下
    python scripts/fetch_ffmpeg.py --url <zip 或 tar.xz 的地址>   # 走内网镜像

为什么是脚本而不是把二进制提交进仓库：一份 Windows 构建解开有 ~150MB，
三个平台就是半个 G——那不该进 git。`.gitignore` 里已经忽略 `bin/ffmpeg*`，
打包时由 CI / 打包机跑一次这个脚本，产物交给 Tauri 的 externalBin 分发。

只做一件事，且做完要能验证：下载 → 解包 → 只取需要的两个可执行文件 →
`-version` 跑一次确认真的能执行。任何一步失败都打印能照着做的下一步，
不留一个「不知道成没成」的中间状态。
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BIN_DIR = REPO_ROOT / "bin"
TOOLS = ("ffmpeg", "ffprobe")

#: 各平台的官方静态构建。都是「最新版」滚动地址，所以不钉 sha256——
#: 校验方式是解包后真的执行一次 `-version`（见 verify）。
SOURCES: dict[str, str] = {
    "windows": "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
    "linux": "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz",
    "darwin": "https://evermeet.cx/ffmpeg/getrelease/zip",
}


def platform_key() -> str:
    system = platform.system().lower()
    if system.startswith("win"):
        return "windows"
    if system == "darwin":
        return "darwin"
    return "linux"


def download(url: str, target: Path) -> None:
    print(f"下载 {url}")
    try:
        with urllib.request.urlopen(url, timeout=120) as resp, target.open("wb") as fh:  # noqa: S310
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            while chunk := resp.read(1 << 20):
                fh.write(chunk)
                done += len(chunk)
                if total:
                    print(f"\r  {done / 1e6:.1f} / {total / 1e6:.1f} MB", end="", flush=True)
        print()
    except urllib.error.URLError as exc:
        raise SystemExit(
            f"下载失败：{exc}\n"
            "  · 确认网络可达，或用 --url 指向内网镜像 / 已下载好的压缩包\n"
            "  · 也可以手工把 ffmpeg 与 ffprobe 放进 bin/，本脚本只是个便利工具"
        ) from exc


def extract(archive: Path, into: Path) -> None:
    if archive.suffix == ".zip" or zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(into)
        return
    with tarfile.open(archive) as tf:
        tf.extractall(into)  # noqa: S202 - 官方构建包，且解到临时目录


def collect(tree: Path, exe_suffix: str) -> dict[str, Path]:
    """在解包结果里找那两个可执行文件——各家构建的目录层级不一样，直接搜。"""
    found: dict[str, Path] = {}
    for tool in TOOLS:
        wanted = f"{tool}{exe_suffix}"
        for path in tree.rglob(wanted):
            if path.is_file():
                found[tool] = path
                break
    return found


def verify(path: Path) -> str:
    """跑一次 `-version`。放不进去就算下载成功也等于没有，必须现在就知道。"""
    proc = subprocess.run(  # noqa: S603
        [str(path), "-version"], capture_output=True, text=True, timeout=30
    )
    if proc.returncode != 0:
        raise SystemExit(f"{path.name} 无法执行（退出码 {proc.returncode}）：{proc.stderr[:400]}")
    return (proc.stdout or "").splitlines()[0] if proc.stdout else path.name


def host_triple() -> str | None:
    """Tauri 的 externalBin 要求文件名带 target triple，由 rustc 报告本机的那个。"""
    try:
        proc = subprocess.run(  # noqa: S603
            ["rustc", "-vV"], capture_output=True, text=True, timeout=20  # noqa: S607
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    for line in proc.stdout.splitlines():
        if line.startswith("host:"):
            return line.split(":", 1)[1].strip()
    return None


def stage_for_tauri(exe_suffix: str) -> None:
    """把 bin/ 里的两个文件按 `<tool>-<triple>` 复制进 tauri/bin/。

    Tauri 打包时会把 triple 去掉，装出来就是主程序旁边的 `ffmpeg(.exe)`——
    正好是后端 `bundle_dirs()` 第一个去找的位置。
    """
    triple = host_triple()
    if not triple:
        print("跳过 tauri/bin：没有 rustc，拿不到 target triple（打包机上再跑一次即可）")
        return
    target = REPO_ROOT / "tauri" / "bin"
    target.mkdir(parents=True, exist_ok=True)
    for tool in TOOLS:
        src = BIN_DIR / f"{tool}{exe_suffix}"
        dst = target / f"{tool}-{triple}{exe_suffix}"
        shutil.copy2(src, dst)
        if os.name != "nt":
            dst.chmod(0o755)
        print(f"就位：{dst.relative_to(REPO_ROOT)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="下载内置 FFmpeg 到 bin/")
    parser.add_argument("--url", help="压缩包地址或本地文件路径；默认按平台选官方构建")
    parser.add_argument("--force", action="store_true", help="已存在也重新下载")
    parser.add_argument(
        "--for-tauri",
        action="store_true",
        help="同时按 <tool>-<target-triple> 复制进 tauri/bin/，供打包时 externalBin 分发",
    )
    args = parser.parse_args()

    key = platform_key()
    exe_suffix = ".exe" if key == "windows" else ""
    BIN_DIR.mkdir(parents=True, exist_ok=True)

    existing = [t for t in TOOLS if (BIN_DIR / f"{t}{exe_suffix}").is_file()]
    if len(existing) == len(TOOLS) and not args.force:
        for tool in TOOLS:
            print(f"已存在：{verify(BIN_DIR / f'{tool}{exe_suffix}')}")
        if args.for_tauri:
            stage_for_tauri(exe_suffix)
        print("加 --force 可以重新下载。")
        return 0

    source = args.url or SOURCES[key]
    with tempfile.TemporaryDirectory(prefix="aivs-ffmpeg-") as tmp:
        tmpdir = Path(tmp)
        # --url 也接受一个已经下载好的本地压缩包：离线机器同样能装。
        local = Path(source)
        if local.is_file():
            archive = local
            print(f"用本地压缩包 {archive}")
        else:
            archive = tmpdir / "ffmpeg-archive"
            download(source, archive)
        print("解包…")
        tree = tmpdir / "tree"
        tree.mkdir()
        extract(archive, tree)

        found = collect(tree, exe_suffix)
        missing = [t for t in TOOLS if t not in found]
        if missing:
            raise SystemExit(
                f"压缩包里没找到：{', '.join(missing)}\n"
                "  · macOS 的 evermeet 分开发布 ffmpeg 与 ffprobe，需要各跑一次 --url\n"
                "  · 或换一个包含完整构建的地址"
            )
        for tool, src in found.items():
            dst = BIN_DIR / f"{tool}{exe_suffix}"
            shutil.copy2(src, dst)
            if os.name != "nt":
                dst.chmod(0o755)
            print(f"就位：{dst.relative_to(REPO_ROOT)} · {verify(dst)}")

    if args.for_tauri:
        stage_for_tauri(exe_suffix)

    print(
        "\n完成。后端会自动优先用 bin/ 里这份（见 backend/app/core/ffmpeg.py），"
        "不需要改任何配置。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

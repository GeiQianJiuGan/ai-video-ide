"""一条命令出 Docker 镜像 tar：镜像名、版本号、落点只有一份口径。

用法（在仓库根目录）：

    python scripts/package-docker.py                  # all-in-one 单镜像 → dist/docker/*.tar
    python scripts/package-docker.py --target split   # 前后端两个镜像装进一个 tar（配 compose）
    python scripts/package-docker.py --target both
    python scripts/package-docker.py --check          # 只体检 + 报计划，一个字节都不写
    python scripts/package-docker.py --version 0.2.0  # 只给这次打包用，仓库里的文件一个不改
    python scripts/package-docker.py --sync-version   # 把散落四处的版本号对齐到源头
    python scripts/package-docker.py --gzip --no-cache --platform linux/amd64

为什么要有这份脚本：`docker build` 不带 `-t` 打出来的镜像叫 `<none>`，`docker compose build`
按目录名瞎起名（仓库里那个 `xunjie_video_ide-backend:latest` 就是这么来的），
`docs/docker-deployment.md` 里手敲的又是 `aivs-allinone:latest`——于是「这个 tar 里装的是什么、
哪个版本」每次都不一样。这里把三件事钉死：

  · **镜像名只有 IMAGES 那三条**（`aivs-allinone` / `aivs-backend` / `aivs-frontend`），
    别处一个名字都不许再写；
  · **版本号只有一个源头** `tauri/tauri.conf.json`（安装包嵌的就是它），其余三处
    （`frontend/package.json` · `backend/pyproject.toml` · `backend/app/core/config.py`）
    对不上就报错，而不是替用户挑一个；
  · **落点固定** `dist/docker/<镜像>-<版本>-<平台>.tar`，旁边一份同名 `.json` 写清里面到底有
    哪些 tag、是哪个 commit 打的——产物自己说得清自己是什么。

同一个版本号同时进三处（镜像 tag、OCI 标签、容器里的 `AIVS_VERSION`），所以 tar 文件名、
`docker images` 那一列、跑起来的容器（后端 `/health` 报的就是 `AIVS_VERSION`）说的是同一个数。

**导出之前每个镜像会真跑一次**（照 `scripts/build_sidecar.py` 的老规矩）：探静态首页与
`/api/v1/health`，核对容器自报的版本号。`--check` 只看得到构建前的环境，逮不到「镜像建成了
但容器起不来」那一类，而那种 tar 搬到服务器上才发现的代价最大。`--no-smoke` 能关掉，
但回执里会如实写上「未自检」。

平台只进文件名不进构建参数：`--platform` 没显式给时按 daemon 报的架构命名，**不替用户
交叉编译**（那要 buildx + QEMU，失败信息又长又难认）。amd64 的 tar 拿到 arm64 机器上
`docker load` 会得到一个跑不起来的容器，所以架构必须写在名字上。
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

REPO_ROOT = Path(__file__).resolve().parents[1]
DIST_DIR = REPO_ROOT / "dist" / "docker"
SOURCE_URL = "https://github.com/GeiQianJiuGan/ai-video-ide"


@dataclass(frozen=True)
class Image:
    """一个镜像的全部身份。镜像名在整个仓库里只出现在这里（与 docker-compose.yml 对齐）。"""

    key: str
    repo: str
    dockerfile: str
    title: str
    #: 要不要把版本号当 build-arg 传进去。只有跑后端的镜像认 `AIVS_VERSION`
    #: （纯 Nginx 的前端镜像里没有 Settings，传了只会换来一句 UnusedBuildArgs 警告）。
    versioned: bool = True
    #: 启动自检：容器里监听的端口。0 = 这个镜像不做自检（原因写在 `no_smoke_why` 里）。
    port: int = 0
    #: 回一个带 version 的 JSON 的路径（只有跑后端的镜像有）。
    health: str = ""
    #: 静态首页的路径，探它是为了确认前端产物真的进了镜像。
    home: str = ""
    #: 不做自检的原因。留空 + port=0 是不允许的组合：绝不静默跳过一次自检。
    no_smoke_why: str = ""


ALLINONE = Image(
    "allinone",
    "aivs-allinone",
    "Dockerfile",
    "AI Video Studio（前端 + 后端 + FFmpeg 单容器）",
    port=80,
    health="/api/v1/health",
    home="/",
)
BACKEND = Image(
    "backend",
    "aivs-backend",
    "backend/Dockerfile",
    "AI Video Studio 后端",
    port=8765,
    health="/api/v1/health",
)
FRONTEND = Image(
    "frontend",
    "aivs-frontend",
    "frontend/Dockerfile",
    "AI Video Studio 前端（Nginx 静态托管 + 反代）",
    versioned=False,
    no_smoke_why=(
        "前端镜像单独跑不起来：nginx 在启动时就要解析 `proxy_pass http://backend:8765` 里那个"
        "上游名字，没有后端容器时直接 emerg 退出。它只在 compose 网络里成立，所以这一次不自检。"
    ),
)

#: 一次打包出哪些镜像、装进哪个 tar。tar 名字的前缀也在这张表里，别在别处拼第二遍。
GROUPS: dict[str, tuple[str, tuple[Image, ...]]] = {
    "allinone": ("aivs-allinone", (ALLINONE,)),
    "split": ("aivs-compose", (BACKEND, FRONTEND)),
}

#: 版本号散落的四处：(相对路径, 取值 / 改写用的正则)。**第一条是源头**，其余三条跟它对齐。
VERSION_FILES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("tauri/tauri.conf.json", re.compile(r'("version"\s*:\s*")([^"]+)(")')),
    ("frontend/package.json", re.compile(r'("version"\s*:\s*")([^"]+)(")')),
    ("backend/pyproject.toml", re.compile(r'(^version\s*=\s*")([^"]+)(")', re.M)),
    ("backend/app/core/config.py", re.compile(r'(^\s*version:\s*str\s*=\s*")([^"]+)(")', re.M)),
)

#: docker tag 的合法字符集（首字符不能是 `.` 或 `-`）。版本号要当 tag 用，先按这个把关。
TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$")


def die(title: str, *suggestions: str) -> NoReturn:
    """照仓库的老规矩：失败必须说清是什么、怎么办，绝不只丢一个退出码。"""
    lines = [f"打包中止：{title}"]
    lines += [f"  · {s}" for s in suggestions]
    raise SystemExit("\n".join(lines))


def step(index: int, total: int, title: str) -> None:
    print(f"\n=== [{index}/{total}] {title} " + "=" * max(0, 46 - len(title)))


def run(cmd: list[str], what: str, *suggestions: str) -> None:
    print(f"$ {' '.join(cmd)}")
    proc = subprocess.run(cmd, cwd=REPO_ROOT)  # noqa: S603
    if proc.returncode != 0:
        die(f"{what} 失败（退出码 {proc.returncode}）", *suggestions)


def capture(cmd: list[str]) -> str:
    """跑一条只读命令拿它的 stdout；失败回空串（调用方自己决定这算不算问题）。"""
    try:
        proc = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)  # noqa: S603
    except OSError:
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


# --------------------------------------------------------------------------- 版本号


def read_version(rel: str, pattern: re.Pattern[str]) -> str | None:
    path = REPO_ROOT / rel
    if not path.is_file():
        die(f"找不到 {rel}", "这份脚本要在仓库根目录下跑：python scripts/package-docker.py")
    text = path.read_text(encoding="utf-8")
    if rel.endswith(".json"):
        # JSON 就正经解析，别拿正则去猜哪个 "version" 是顶层那个。
        try:
            value = json.loads(text).get("version")
        except json.JSONDecodeError:
            value = None
        if isinstance(value, str):
            return value
    match = pattern.search(text)
    return match.group(2) if match else None


def version_table() -> list[tuple[str, str | None]]:
    return [(rel, read_version(rel, pattern)) for rel, pattern in VERSION_FILES]


def write_version(rel: str, pattern: re.Pattern[str], version: str) -> None:
    """只改 version 那一行，其余一个字节都不动（所以是正则替换而不是重新序列化）。"""
    path = REPO_ROOT / rel
    text = path.read_text(encoding="utf-8")
    new, count = pattern.subn(rf"\g<1>{version}\g<3>", text, count=1)
    if count != 1:
        die(
            f"{rel} 里没找到能改的 version 那一行",
            "手动改一下，或者用 --version <版本> 只给这次打包指定",
        )
    if rel.endswith(".json"):
        # 改完先自检一遍：确认动的是顶层那个 version，而不是碰巧长得像的另一处。
        try:
            hit = json.loads(new).get("version")
        except json.JSONDecodeError:
            hit = None
        if hit != version:
            die(f"{rel} 改到了别的地方（顶层 version 还不是 {version}）", "手动改这一处")
    path.write_text(new, encoding="utf-8")


def resolve_version(explicit: str | None, sync: bool) -> tuple[str, str]:
    """定下这次打包用哪个版本号。返回 (版本号, 它是谁给的)。

    源头是 `VERSION_FILES[0]`。其余三处对不上时**报错而不是替用户挑一个**——
    「版本号不一致」本身就是这份脚本要修的毛病，静悄悄选一个只会把它藏起来。
    """
    table = version_table()
    source_rel, source = table[0]
    version = explicit or source
    if not version:
        die(f"{source_rel} 里读不出 version", "手动指定：--version 0.1.0")
    if not TAG_RE.match(version):
        die(
            f"版本号 {version!r} 不能当 docker tag 用",
            "只允许字母 / 数字 / . / _ / -，首字符必须是字母或数字",
        )

    if sync:
        drift = [(rel, got) for rel, got in table if got != version]
        stale = {rel for rel, _ in drift}
        for rel, pattern in VERSION_FILES:
            if rel in stale:
                write_version(rel, pattern, version)
        print(f"版本号已对齐到 {version}：")
        for rel, got in drift:
            print(f"  {rel:<30} {got} → {version}")
        if not drift:
            print(f"  四处本来就都是 {version}，没什么要改的")
        return version, "--sync-version"

    if explicit:
        if source and source != explicit:
            print(
                f"注意：--version {explicit} 与源头 {source_rel} 里的 {source} 不同；"
                "仓库里的文件一个都不改，只有这次的镜像 tag 用 --version 给的这个数。"
            )
        return explicit, "--version"

    drift = [(rel, got) for rel, got in table[1:] if got != version]
    if drift:
        die(
            f"版本号有 {len(drift)} 处对不上源头（{source_rel} = {version}）",
            *[f"{rel} 写的是 {got or '读不出来'}" for rel, got in drift],
            "对齐它们：python scripts/package-docker.py --sync-version",
            "或只给这次打包指定一个：python scripts/package-docker.py --version <版本>",
        )
    return version, source_rel


# --------------------------------------------------------------------------- docker


def docker_exe() -> str:
    exe = shutil.which("docker")
    if not exe:
        die(
            "找不到 docker 命令",
            "装 Docker：https://docs.docker.com/get-docker/",
            "Windows / macOS 上装完还要把 Docker Desktop 打开（守护进程不起来什么都构建不了）",
        )
    return exe


def daemon_platform(exe: str) -> str:
    """daemon 自己报的 `os/arch`。连不上回空串——由 doctor 归成一条能看懂的缺失项。"""
    return capture([exe, "version", "--format", "{{.Server.Os}}/{{.Server.Arch}}"])


def image_meta(exe: str, tag: str) -> tuple[str, int]:
    raw = capture([exe, "image", "inspect", "--format", "{{.Id}} {{.Size}}", tag])
    if not raw:
        return "", 0
    parts = raw.split()
    try:
        return parts[0], int(parts[1])
    except (IndexError, ValueError):
        return parts[0] if parts else "", 0


def git_revision() -> str:
    """哪个 commit 打的。工作区脏就加 `-dirty`——不然 tar 说的话是假的。"""
    rev = capture(["git", "rev-parse", "--short", "HEAD"]) or "unknown"
    if capture(["git", "status", "--porcelain"]):
        rev += "-dirty"
    return rev


def doctor(exe: str, images: tuple[Image, ...]) -> list[str]:
    """体检。返回缺失项，空列表才算能构建。"""
    problems: list[str] = []
    print("环境体检：")
    print(f"  {'docker':<24} {exe}")

    server = daemon_platform(exe)
    print(f"  {'daemon':<24} {server or '连不上'}")
    if not server:
        problems.append(
            "连不上 Docker 守护进程 —— 打开 Docker Desktop，"
            "或 Linux 上 sudo systemctl start docker（普通用户还要在 docker 组里）"
        )

    for image in images:
        ok = (REPO_ROOT / image.dockerfile).is_file()
        print(f"  {image.dockerfile:<24} {'ok' if ok else '缺失'}")
        if not ok:
            problems.append(f"缺少 {image.dockerfile} —— 这个镜像没法构建")

    # 三份 Dockerfile 都 COPY 它；不在的话 docker build 会在 COPY 那一步才炸。
    workflows = REPO_ROOT / "workflows"
    print(f"  {'workflows/':<24} {'ok' if workflows.is_dir() else '缺失（会自动建一个空目录）'}")
    return problems


def ensure_build_context() -> list[str]:
    """把 Dockerfile 里 COPY 得到的东西摆齐。返回要如实报出来的降级说明。"""
    notes: list[str] = []
    workflows = REPO_ROOT / "workflows"
    if not workflows.is_dir():
        workflows.mkdir(parents=True)
        (workflows / ".gitkeep").write_text("", encoding="utf-8")
        notes.append("workflows/ 不在，已建一个空目录（三份 Dockerfile 都 COPY 它）")

    ignore = REPO_ROOT / ".dockerignore"
    if ignore.is_file():
        entries = {line.strip() for line in ignore.read_text(encoding="utf-8").splitlines()}
        if "dist" not in entries and "dist/" not in entries:
            notes.append(
                ".dockerignore 里没有 dist —— 上次打的 tar 会被当成构建上下文传给 daemon，"
                "越打越慢；建议把 dist 加进去"
            )
    return notes


# --------------------------------------------------------------------------- 构建与导出


def tags_of(image: Image, version: str, latest: bool) -> list[str]:
    tags = [f"{image.repo}:{version}"]
    if latest and version != "latest":
        # `:latest` 是给 docker-compose.yml 与「随手 docker run 一下」用的浮动标签，
        # 带版本那个才是归档用的。同一个镜像两个 tag，docker save 只会存一份层。
        tags.append(f"{image.repo}:latest")
    return tags


def build(
    exe: str,
    image: Image,
    version: str,
    *,
    revision: str,
    created: str,
    platform: str | None,
    latest: bool,
    no_cache: bool,
    pull: bool,
) -> list[str]:
    tags = tags_of(image, version, latest)
    cmd = [exe, "build", "-f", image.dockerfile]
    for tag in tags:
        cmd += ["-t", tag]
    # OCI 标签：这样「这个镜像是哪个版本、哪个 commit」不用翻文件名也能问出来
    # （docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.version"}}'
    # 就够了，不必信文件名）。
    for key, value in {
        "org.opencontainers.image.title": image.title,
        "org.opencontainers.image.version": version,
        "org.opencontainers.image.revision": revision,
        "org.opencontainers.image.created": created,
        "org.opencontainers.image.source": SOURCE_URL,
        "org.opencontainers.image.licenses": "MIT",
    }.items():
        cmd += ["--label", f"{key}={value}"]
    if image.versioned:
        # 传进容器里的 AIVS_VERSION，后端 /health 报的就是它——镜像 tag 说 0.2.0 而
        # 界面上写 0.1.0 也算「版本名不一致」，所以这里一起钉死。
        cmd += ["--build-arg", f"AIVS_VERSION={version}"]
    if platform:
        cmd += ["--platform", platform]
    if no_cache:
        cmd.append("--no-cache")
    if pull:
        cmd.append("--pull")
    cmd.append(".")
    run(
        cmd,
        f"构建 {image.repo}:{version}",
        "npm / pip 那几步失败多半是网络，重跑一次或配好镜像源再来",
        f"想看完整日志：docker build -f {image.dockerfile} -t {image.repo}:{version} "
        ". --progress plain",
    )
    return tags


# --------------------------------------------------------------------------- 启动自检


def _http_get(url: str, timeout: float = 3.0) -> tuple[int, str]:
    """探一次本机那个临时容器。连不上回 `(0, 原因)`，不抛。"""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            return int(resp.status), resp.read(4096).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return int(exc.code), str(exc.reason)
    except (OSError, urllib.error.URLError) as exc:  # 容器还没起来 / 端口没通
        return 0, str(exc)


def _host_port(exe: str, name: str, port: int) -> str:
    """容器端口映射到本机哪个端口。用 `-p 127.0.0.1::` 让 daemon 自己挑，免得撞上占用的端口。"""
    for line in capture([exe, "port", name, f"{port}/tcp"]).splitlines():
        tail = line.rpartition(":")[2].strip()
        if tail.isdigit():
            return tail
    return ""


def _running(exe: str, name: str) -> bool:
    return capture([exe, "inspect", "-f", "{{.State.Running}}", name]) == "true"


def _container_logs(exe: str, name: str) -> list[str]:
    """容器留下的输出。docker 把不少东西写到 stderr，所以两条流一起收。"""
    try:
        proc = subprocess.run(  # noqa: S603
            [exe, "logs", "--tail", "20", name],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            errors="replace",
        )
    except OSError:
        return []
    return (proc.stdout + proc.stderr).strip().splitlines()


def smoke(exe: str, image: Image, tag: str, version: str) -> list[str]:
    """把刚打出来的镜像真跑一次，回几句写进回执的自检结论。

    照 `scripts/build_sidecar.py` 的老规矩：产物必须自己启动过一次才算打完。`--check` 那一遍
    只看得到构建前的环境，逮不到「镜像建成了但容器起不来」这一类——比如 Windows 上检出的
    `docker/entrypoint.sh` 带着 CRLF 时，内核照 `#!/bin/bash\\r` 去找解释器，容器只报一句
    `exec /app/entrypoint.sh: no such file or directory`（文件其实就在那儿）。
    顺手核一遍容器自报的版本号：tag 说 0.2.0 而 `/health` 说 0.1.0 正是这份脚本要治的毛病。
    """
    if not image.port:
        print(f"跳过自检：{image.no_smoke_why}")
        return [f"未自检 · {image.no_smoke_why}"]

    name = f"aivs-smoke-{image.key}"
    # 上一次中断留下的同名容器；没有也不算问题，所以不看返回码。
    subprocess.run(  # noqa: S603
        [exe, "rm", "-f", name], cwd=REPO_ROOT, capture_output=True
    )
    print(f"$ {exe} run -d --name {name} -p 127.0.0.1::{image.port} {tag}")
    started = capture([exe, "run", "-d", "--name", name, "-p", f"127.0.0.1::{image.port}", tag])
    if not started:
        die(
            f"{tag} 连容器都创建不起来",
            f"自己看一眼：docker run --rm -p 8080:{image.port} {tag}",
            "确认 daemon 还活着、本机还有磁盘空间",
        )

    lines: list[str] = []
    deadline = time.monotonic() + 90
    try:
        # 起来就退了的容器 `docker port` 什么都不回（CRLF 那个 exec 失败就是这样），所以要分清
        # 「端口还没映射好」与「容器已经死了」——两句话指向的排查方向完全不同。
        host_port = ""
        for _ in range(10):
            host_port = _host_port(exe, name, image.port)
            if host_port or not _running(exe, name):
                break
            time.sleep(0.5)
        if not host_port:
            alive = _running(exe, name)
            logs = _container_logs(exe, name) or ["（容器什么都没输出）"]
            die(
                f"{tag} " + (f"没把 {image.port} 映射到本机" if alive else "起来就退了"),
                *[f"容器日志：{ln}" for ln in logs[-8:]],
                f"自己复现一遍：docker run --rm -p 8080:{image.port} {tag}",
            )

        def probe(path: str) -> str:
            """等到这条路径回 200；容器提前退了或超时就带着日志报错。

            **502 / 503 不是结论**：all-in-one 里 nginx 先起、uvicorn 后起，那几秒内
            `/api/` 一律是 502 Bad Gateway。除了「容器已经退了」之外都得接着等，
            不然自检逮到的是启动过程而不是启动结果。
            """
            url = f"http://127.0.0.1:{host_port}{path}"
            status, body = 0, "还没探到"
            while time.monotonic() < deadline:
                status, body = _http_get(url)
                if status == 200:
                    break
                if not _running(exe, name):
                    break  # 已经退了，再等也不会变
                time.sleep(1.0)
            if status != 200:
                logs = _container_logs(exe, name) or ["（容器什么都没输出）"]
                die(
                    f"{tag} 的 {path} 不通（{status or '连不上'}：{body.strip()[:160]}）",
                    *[f"容器日志：{ln}" for ln in logs[-8:]],
                    f"自己复现一遍：docker run --rm -p 8080:{image.port} {tag}",
                    "改完再打；真要跳过这一步是 --no-smoke（那 tar 就没人验过）",
                )
            return body

        if image.home:
            probe(image.home)
            print(f"  {image.home} 回 200（前端产物在镜像里）")
            lines.append(f"{image.home} 回 200")
        if image.health:
            try:
                got = json.loads(probe(image.health)).get("version")
            except json.JSONDecodeError:
                got = None
            if got != version:
                die(
                    f"容器自报版本 {got!r} ≠ 镜像 tag {version!r}——这就是「版本名不一致」本身",
                    "Dockerfile 里要有 ARG AIVS_VERSION + ENV AIVS_VERSION=${AIVS_VERSION}",
                    "构建时要传 --build-arg AIVS_VERSION（这份脚本会传，手敲 docker build 不会）",
                )
            print(f"  {image.health} 报 version={got}（与镜像 tag 是同一个数）")
            lines.append(f"{image.health} 报 version={got}")
    finally:
        subprocess.run(  # noqa: S603
            [exe, "rm", "-f", name], cwd=REPO_ROOT, capture_output=True
        )
    return lines


# --------------------------------------------------------------------------- 导出


def save(exe: str, tags: list[str], out: Path, use_gzip: bool) -> None:
    """`docker save` 一路流进文件。

    先写 `.part` 再改名：中途断了留下的是个明显没写完的文件，而不是一个看着像 tar、
    `docker load` 到一半才报错的东西。
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    part = out.with_name(out.name + ".part")
    cmd = [exe, "save", *tags]
    print(f"$ {' '.join(cmd)} > {out.relative_to(REPO_ROOT).as_posix()}")

    written = 0
    next_mark = 200_000_000
    with subprocess.Popen(cmd, cwd=REPO_ROOT, stdout=subprocess.PIPE) as proc:  # noqa: S603
        assert proc.stdout is not None
        sink = gzip.open(part, "wb") if use_gzip else part.open("wb")
        try:
            while chunk := proc.stdout.read(1 << 20):
                sink.write(chunk)
                written += len(chunk)
                if written >= next_mark:
                    print(f"  已读出 {written / 1e6:.0f} MB …")
                    next_mark += 200_000_000
        finally:
            sink.close()
        code = proc.wait()
    if code != 0:
        part.unlink(missing_ok=True)
        die(
            f"docker save 失败（退出码 {code}）",
            "确认上面几个 tag 真的在 docker images 里",
            "磁盘剩余空间够不够：镜像解开是好几个 GB",
        )
    part.replace(out)


def write_receipt(path: Path, payload: dict[str, object]) -> None:
    """tar 旁边那份回执。产物自己说得清自己是什么，别人不必来问打包的人。"""
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def show(path: Path) -> str:
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(path)


def how_to(group: str, version: str, tar: Path) -> list[str]:
    """搬到目标机器上之后要敲的那几行。跟着 tar 一起写进回执，不靠人记。"""
    load = f"docker load -i {tar.name}"
    if group == "allinone":
        return [
            load,
            "docker run -d --name aivs-app -p 80:80 \\",
            "  -e AIVS_COMFY_BASE_URL=http://<算力机IP>:8188 \\",
            "  -v $(pwd)/data/runtime:/app/.runtime \\",
            "  -v $(pwd)/data/projects:/app/data \\",
            "  -v $(pwd)/workflows:/app/workflows \\",
            f"  --restart unless-stopped {ALLINONE.repo}:{version}",
        ]
    return [
        load,
        "# 镜像已经在本地，别再 --build：那会按目录名重新起一个别的镜像名",
        f"AIVS_VERSION={version} docker compose up -d",
    ]


# --------------------------------------------------------------------------- 入口


def main() -> int:
    parser = argparse.ArgumentParser(
        description="出 Docker 镜像 tar：落在 dist/docker，镜像名与版本号只有一份口径"
    )
    parser.add_argument(
        "--target",
        choices=("allinone", "split", "both"),
        default="allinone",
        help="allinone=前后端单容器（默认）· split=前后端两个镜像装进一个 tar（配 compose）"
        "· both=两样都要",
    )
    parser.add_argument("--version", help="只给这次打包用的版本号；不给就读 tauri/tauri.conf.json")
    parser.add_argument(
        "--sync-version", action="store_true", help="把散落四处的版本号对齐到源头（会改那三个文件）"
    )
    parser.add_argument(
        "--platform", help="传给 docker build 的 --platform；不给就按 daemon 的架构命名，不交叉编译"
    )
    parser.add_argument("--check", action="store_true", help="只体检 + 报计划，一个字节都不写")
    parser.add_argument("--no-latest", action="store_true", help="不额外打 :latest 这个浮动标签")
    parser.add_argument("--no-cache", action="store_true", help="不吃构建缓存，全部重来")
    parser.add_argument("--pull", action="store_true", help="构建前先拉一遍基础镜像的最新版")
    parser.add_argument(
        "--gzip", action="store_true", help="直接压成 .tar.gz（docker load 自己会解）"
    )
    parser.add_argument(
        "--no-smoke",
        action="store_true",
        help="不做启动自检（默认会把镜像真跑一次、核对它自报的版本号）",
    )
    parser.add_argument("--no-save", action="store_true", help="只构建镜像，不导出 tar")
    parser.add_argument("--out", help="改 tar 的落点，默认 dist/docker")
    args = parser.parse_args()

    groups = ["allinone", "split"] if args.target == "both" else [args.target]
    # 同一个镜像被两个 group 要到时只构建一次（dict 保序去重）。
    images = tuple(dict.fromkeys(image for g in groups for image in GROUPS[g][1]))

    version, version_from = resolve_version(args.version, args.sync_version)
    exe = docker_exe()
    problems = doctor(exe, images)

    platform_label = args.platform or daemon_platform(exe) or "unknown/unknown"
    slug = platform_label.replace("/", "-")
    out_dir = Path(args.out).resolve() if args.out else DIST_DIR
    latest = not args.no_latest
    suffix = ".tar.gz" if args.gzip else ".tar"

    # 先账单再动手：构建前把「打什么名字、落到哪」全列出来。
    plan: list[tuple[str, tuple[Image, ...], Path]] = []
    print("\n计划：")
    print(f"  版本号   {version}（来自 {version_from}）")
    print(
        f"  平台     {platform_label}"
        + ("" if args.platform else "（daemon 报的架构，只进文件名）")
    )
    for group in groups:
        stem, group_images = GROUPS[group]
        tar = out_dir / f"{stem}-{version}-{slug}{suffix}"
        plan.append((group, group_images, tar))
        tags = " · ".join(tag for image in group_images for tag in tags_of(image, version, latest))
        print(f"  {group:<8} {tags}")
        print(f"           → {show(tar)}" + ("（--no-save：这次不写）" if args.no_save else ""))
    print(
        "  自检     "
        + (
            "跳过（--no-smoke）"
            if args.no_smoke
            else "每个镜像真跑一次，核对它自报的版本号，跑不起来就不出包"
        )
    )

    if args.check:
        if problems:
            print("\n还差这些：")
            for problem in problems:
                print(f"  · {problem}")
            return 1
        print("\n体检通过，去掉 --check 就能出包。")
        return 0
    if problems:
        die("环境不完整", *problems)

    for note in ensure_build_context():
        print(f"注意：{note}")

    created = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    revision = git_revision()
    total = len(images) + (0 if args.no_smoke else 1) + (0 if args.no_save else len(plan))
    done = 0
    built: dict[str, list[str]] = {}
    for image in images:
        done += 1
        step(done, total, f"构建 {image.repo}:{version}")
        built[image.key] = build(
            exe,
            image,
            version,
            revision=revision,
            created=created,
            platform=args.platform,
            latest=latest,
            no_cache=args.no_cache,
            pull=args.pull,
        )

    # 自检排在导出之前：跑不起来的镜像不值得再花几分钟写一个几百 MB 的 tar。
    checks: dict[str, list[str]] = {}
    if args.no_smoke:
        checks = {image.key: ["未自检 · --no-smoke"] for image in images}
        print("\n注意：--no-smoke，这批镜像没人验过它能不能起来。")
    else:
        done += 1
        step(done, total, "启动自检")
        for image in images:
            checks[image.key] = smoke(exe, image, built[image.key][0], version)

    if args.no_save:
        print("\n镜像已经在本地（--no-save，没有写 tar）：")
        for image in images:
            for tag in built[image.key]:
                print(f"  {tag}")
        return 0

    artifacts: list[tuple[Path, Path]] = []
    for group, group_images, tar in plan:
        done += 1
        step(done, total, f"导出 {tar.name}")
        tags = [tag for image in group_images for tag in built[image.key]]
        save(exe, tags, tar, args.gzip)
        members = []
        for image in group_images:
            image_id, size = image_meta(exe, built[image.key][0])
            members.append(
                {
                    "repo": image.repo,
                    "dockerfile": image.dockerfile,
                    "tags": built[image.key],
                    "id": image_id,
                    "size_bytes": size,
                    "self_check": checks.get(image.key, []),
                }
            )
        receipt = tar.with_name(tar.name.removesuffix(suffix) + ".json")
        write_receipt(
            receipt,
            {
                "kind": "aivs-docker-package",
                "group": group,
                "version": version,
                "version_from": version_from,
                "platform": platform_label,
                "revision": revision,
                "created": created,
                "tar": tar.name,
                "tar_bytes": tar.stat().st_size,
                "gzip": bool(args.gzip),
                "images": members,
                "how_to": how_to(group, version, tar),
            },
        )
        artifacts.append((tar, receipt))

    print("\n产物：")
    for tar, receipt in artifacts:
        print(f"  {show(tar)} · {tar.stat().st_size / 1e6:.1f} MB")
        print(f"  {show(receipt)} · 里面写清了 tag / commit / 怎么跑")
    for group, _, tar in plan:
        print(f"\n{group} 搬到目标机器上之后：")
        for line in how_to(group, version, tar):
            print(f"  {line}")
    print(
        "\n镜像里带了 FFmpeg 与后端依赖，ComfyUI 仍在外部算力机上"
        "（用 AIVS_COMFY_BASE_URL 指过去；没配也能走完手动流程）。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

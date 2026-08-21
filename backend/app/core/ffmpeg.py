"""FFmpeg / FFprobe 定位——内置副本优先，不要求用户自己装。

导出、抽帧、代理转码都靠 FFmpeg，它不该是一道「先去装个东西」的门槛，所以
应用**自带**一份：`scripts/fetch_ffmpeg.py` 把二进制放进 `<repo>/bin`，
打包时由 Tauri 的 externalBin 落到主程序同目录（壳会把那个目录用
`AIVS_BUNDLE_DIR` 告诉 sidecar）。

查找顺序，以及为什么是这个顺序：

1. **显式配置**（`AIVS_FFMPEG_PATH` 写成一个路径）——用户指名要用哪个，
   永远排第一。指了却找不到时**不静默回退**到内置：那是配置写错了，
   得说出来，否则「我明明指了自编译的那个」会变成一桩查不到的怪事。
2. **内置副本**——随应用分发的那份，版本可控，与我们的参数组合对得上。
3. **PATH**——机器上本来就有的。它排最后而不是最前：系统里那份版本未知，
   能用就用，但不该盖掉我们自带的。

刻意不做缓存：用户可能在应用开着的时候才去装或才去下载内置副本，
下一次探测就该看见——一次 `which` 加几个 stat，便宜得不值得缓存。
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from app.core.config import REPO_ROOT, settings
from app.core.errors import AppError, ErrorCode

EXE_SUFFIX = ".exe" if os.name == "nt" else ""

Source = Literal["configured", "bundled", "path"]

#: 支持的两个工具名 → settings 上对应的配置字段。
_CONFIG_FIELD = {"ffmpeg": "ffmpeg_path", "ffprobe": "ffprobe_path"}

#: 给人看的名字。错误标题里写「找不到 FFmpeg」而不是内部的小写工具名。
_DISPLAY = {"ffmpeg": "FFmpeg", "ffprobe": "FFprobe"}


@dataclass(frozen=True)
class Located:
    """一次查找的完整结果。找不到时也要能说清「我找过哪里」。"""

    tool: str
    path: str | None
    source: Source | None
    searched: list[str] = field(default_factory=list)
    #: 配置里指了一个路径，但那里没有可执行文件——这是配置错误，不能悄悄回退。
    configured_missing: str = ""

    @property
    def available(self) -> bool:
        return self.path is not None


def bundle_dirs() -> list[Path]:
    """内置副本可能落在哪里，按可信度排序。

    - `AIVS_BUNDLE_DIR`：Tauri 壳启动 sidecar 时注入的主程序目录（externalBin 的落点）；
    - 冻结后的 sidecar 自身目录：没有壳注入时（直接双击 sidecar）也要能找到；
    - `<repo>/bin`：开发期 `scripts/fetch_ffmpeg.py` 的下载目标。
    """
    out: list[Path] = []
    if settings.bundle_dir:
        out += [settings.bundle_dir, settings.bundle_dir / "bin"]
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        out += [exe_dir, exe_dir / "bin"]
    out.append(REPO_ROOT / "bin")
    seen: set[Path] = set()
    uniq: list[Path] = []
    for d in out:
        if d not in seen:
            seen.add(d)
            uniq.append(d)
    return uniq


def locate(tool: str = "ffmpeg") -> Located:
    """按「显式配置 → 内置 → PATH」找一个工具。找不到不抛，只如实汇报。"""
    configured = getattr(settings, _CONFIG_FIELD[tool], tool)
    searched: list[str] = []

    # 1. 显式配置：只有写成路径（含分隔符）才算「指名」，裸名字仍是默认值。
    explicit = configured != tool and (os.sep in configured or "/" in configured)
    if explicit:
        searched.append(configured)
        hit = _executable(Path(configured))
        if hit:
            return Located(tool, hit, "configured", searched)
        return Located(tool, None, None, searched, configured_missing=configured)

    # 2. 内置副本。
    for d in bundle_dirs():
        candidate = d / f"{tool}{EXE_SUFFIX}"
        searched.append(str(candidate))
        hit = _executable(candidate)
        if hit:
            return Located(tool, hit, "bundled", searched)

    # 3. 机器上本来就有的那份。
    searched.append(f"PATH: {configured}")
    found = shutil.which(configured)
    if found:
        return Located(tool, found, "path", searched)
    return Located(tool, None, None, searched)


def _executable(path: Path) -> str | None:
    """存在且是文件就算可用。Windows 上没有 +x 位，权限检查交给实际执行去报错。"""
    if not path.is_file():
        return None
    if os.name != "nt" and not os.access(path, os.X_OK):
        return None
    return str(path.resolve())


#: 找不到时的统一建议。第一条永远是「用内置的那份」——那才是设计的默认路径。
FETCH_HINT = "在仓库根目录运行 python scripts/fetch_ffmpeg.py 下载内置副本（存到 bin/）"


def require(tool: str = "ffmpeg") -> str:
    """拿到可执行路径，拿不到就抛 `FFMPEG_MISSING`（带可执行的下一步）。"""
    found = locate(tool)
    if found.path:
        return found.path
    env_key = f"AIVS_{_CONFIG_FIELD[tool].upper()}"
    name = _DISPLAY.get(tool, tool)
    if found.configured_missing:
        raise AppError(
            ErrorCode.FFMPEG_MISSING,
            f"配置指定的 {name} 不存在",
            f"{env_key} = {found.configured_missing}，那里没有可执行文件。"
            "显式指定的路径不会被静默忽略，所以内置副本与 PATH 都没有去找。",
            [
                "修正这个路径，或删掉该配置改用内置副本",
                FETCH_HINT,
                f"或清空 {env_key} 后重启后端",
            ],
            {"searched": found.searched},
        )
    raise AppError(
        ErrorCode.FFMPEG_MISSING,
        f"找不到 {name}",
        f"内置副本与 PATH 里都没有 {tool}{EXE_SUFFIX}。应用本该自带一份，这台机器上还没有。",
        [
            FETCH_HINT,
            "或安装 FFmpeg 并加入 PATH",
            f"或用 {env_key} 指向可执行文件的绝对路径",
        ],
        {"searched": found.searched},
    )


#: 给状态栏 / 概览页用的来源说明——「内置」和「你机器上那份」不是一回事。
SOURCE_LABEL: dict[str, str] = {
    "configured": "配置指定",
    "bundled": "内置",
    "path": "系统 PATH",
}

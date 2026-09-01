"""打包成 sidecar 之后那几条路径还对不对。

冻结后 `__file__` 落在 PyInstaller 的临时解包目录里，于是「往上数几级」这种算法
会指到系统临时目录——`bin/` 不在那儿，`.runtime/` 更不该往那儿写，而且每次启动
路径都变。两个入口各有一套算法，方向还相反，所以都得盯着：

- `config._repo_root()` → **可执行文件所在目录**（externalBin 把 ffmpeg 装在那一层）
- `migrate._backend_root()` → **解包目录 `sys._MEIPASS`**（迁移脚本作为数据文件摆在那儿）

外加一条静态检查：spec 里 datas 的落点必须和 `_backend_root()` 说的是同一个地方，
否则打出来的后端能启动、但一建工程就找不到迁移脚本——那是装完双击才暴露的失败。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.core import config
from app.persistence import migrate

REPO = Path(__file__).resolve().parents[2]
SPEC = REPO / "backend" / "aivs-backend.spec"


def test_repo_root_in_source_tree_is_the_repo() -> None:
    assert config._repo_root() == REPO
    assert (config._repo_root() / "backend" / "pyproject.toml").is_file()


def test_repo_root_when_frozen_is_the_executable_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exe = tmp_path / "install" / "aivs-backend.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    monkeypatch.setattr(config.sys, "frozen", True, raising=False)
    monkeypatch.setattr(config.sys, "executable", str(exe))

    # 主程序、sidecar、ffmpeg 是同一层——bundle_dirs() 就是靠这个假设找内置 FFmpeg。
    assert config._repo_root() == exe.parent


def test_backend_root_when_frozen_is_the_unpack_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(migrate.sys, "frozen", True, raising=False)
    monkeypatch.setattr(migrate.sys, "_MEIPASS", str(tmp_path), raising=False)

    assert migrate._backend_root() == tmp_path


def test_backend_root_falls_back_to_exe_dir_without_meipass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """onedir 模式没有 `_MEIPASS`，此时数据文件就在可执行文件旁边。"""
    exe = tmp_path / "aivs-backend"
    exe.write_bytes(b"")
    monkeypatch.setattr(migrate.sys, "frozen", True, raising=False)
    monkeypatch.delattr(migrate.sys, "_MEIPASS", raising=False)
    monkeypatch.setattr(migrate.sys, "executable", str(exe))

    assert migrate._backend_root() == tmp_path


def test_spec_ships_alembic_where_backend_root_looks_for_it() -> None:
    spec = SPEC.read_text(encoding="utf-8")
    # alembic.ini 落 bundle 根，迁移脚本落 alembic/versions —— 与 _backend_root() 对得上。
    assert '"alembic.ini"), "."' in spec
    assert '"alembic/versions"' in spec
    assert '"env.py"), "alembic"' in spec


def test_every_revision_file_is_registered() -> None:
    """spec 是按 `versions/*.py` 通配打包的，所以漏的不会是文件而是登记。

    `REVISION_SCHEMA` 少一条，用户打开旧工程时就看不到「schema X → Y」。
    """
    ids = {
        m.group(1)
        for path in (REPO / "backend" / "alembic" / "versions").glob("*.py")
        if (
            m := re.search(
                r'^revision(?::\s*str)?\s*=\s*["\'](.+)["\']',
                path.read_text(encoding="utf-8"),
                re.MULTILINE,
            )
        )
    }
    assert ids, "alembic/versions 下没有迁移脚本？"
    assert ids == set(migrate.REVISION_SCHEMA), (
        "迁移脚本与 REVISION_SCHEMA 对不上："
        f"多出来的脚本 {sorted(ids - set(migrate.REVISION_SCHEMA))}、"
        f"登记了却没有脚本 {sorted(set(migrate.REVISION_SCHEMA) - ids)}"
    )

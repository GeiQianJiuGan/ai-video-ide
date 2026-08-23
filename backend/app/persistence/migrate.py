"""工程数据库的迁移与识别。

两件事：
  1. 打开工程时把 project.db 升到最新 revision（幂等，可反复调用）。
  2. 在动手之前先判断「这个 project.db 到底是不是我们的」——
     不是就报错，绝不覆盖用户的文件。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine

from alembic import command
from app.core.logging import get_logger

log = get_logger("migrate")

BACKEND_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"
ALEMBIC_DIR = BACKEND_ROOT / "alembic"

#: 每个 revision 落地后工程处于哪个 schema 版本。新增迁移必须在这里登记，
#: 否则打开旧工程时无法向用户显示「schema X → Y」。
REVISION_SCHEMA: dict[str, int] = {
    "0001_project": 1,
    "0002_domain": 2,
    "0003_library_origin": 3,
    "0004_scene_flow": 4,
    "0005_scene_nodes": 5,
    "0006_shot_adopted_video": 6,
    "0007_timeline_audio": 7,
    "0008_global_workflow_bindings": 8,
    "0009_project_generation_mode": 9,
    "0010_project_preset": 10,
    "0011_project_video_presets": 11,
    "0012_shot_link": 12,
}


def _config(db_path: Path) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    cfg.set_main_option("aivs_db", db_path.as_posix())
    cfg.attributes["aivs_embedded"] = True
    return cfg


def head_revision() -> str:
    script = ScriptDirectory(str(ALEMBIC_DIR))
    head = script.get_current_head()
    assert head is not None, "alembic/versions 下没有任何迁移脚本"
    return head


def current_revision(db_path: Path) -> str | None:
    """读取 project.db 当前 revision；库不存在或没被迁移过时返回 None。"""
    if not db_path.exists():
        return None
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        with engine.connect() as conn:
            return MigrationContext.configure(conn).get_current_revision()
    finally:
        engine.dispose()


def upgrade_to_head(db_path: Path) -> tuple[str | None, str]:
    """把库升到 head，返回 (升级前 revision, head)。阻塞调用，请放进线程。"""
    before = current_revision(db_path)
    head = head_revision()
    if before != head:
        command.upgrade(_config(db_path), "head")
        log.info("db.migrated", path=str(db_path), from_rev=before, to_rev=head)
    return before, head


def table_names(db_path: Path) -> set[str]:
    """用 stdlib sqlite3 直读表名：连不上就说明这不是一个 SQLite 文件。"""
    try:
        with sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True) as conn:
            rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        return {r[0] for r in rows}
    except sqlite3.Error:
        return set()


def is_our_db(db_path: Path) -> bool:
    """是否是本应用的工程库：必须同时有 alembic_version 与 project 表。"""
    names = table_names(db_path)
    return "alembic_version" in names and "project" in names

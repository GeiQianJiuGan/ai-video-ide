"""M1 · 项目容器验收。

覆盖三件事：
  1. 新建 / 打开 / 最近列表的正常路径，磁盘布局与数据库内容都要对得上；
  2. 冲突与损坏路径必须是结构化错误 + 可执行建议（绝不静默、绝不覆盖）；
  3. schema 落后的工程被打开时自动升级，并把 from → to 报出来。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.events.bus import Channel, bus
from app.persistence import migrate
from app.services.projects import DB_NAME, MANIFEST_NAME, SUBDIRS, projects


@pytest.fixture(autouse=True)
async def isolated_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """recent.json 是应用级状态，测试之间必须隔离；结束时关掉所有已打开的库。"""
    monkeypatch.setattr(settings, "runtime_dir", tmp_path / "runtime")
    settings.runtime_dir.mkdir(parents=True, exist_ok=True)
    await projects.close_all()
    yield
    await projects.close_all()


def make_project(client: TestClient, directory: Path, name: str = "我的短片") -> dict:
    resp = client.post(
        "/api/v1/projects",
        json={
            "dir": str(directory),
            "name": name,
            "width": 1920,
            "height": 1080,
            "fps": 25,
            "duration_unit": "frames",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def error_of(resp) -> dict:  # noqa: ANN001
    body = resp.json()
    assert "error" in body, f"不是结构化错误：{body}"
    err = body["error"]
    assert err["title"] and err["detail"], "错误缺少 title / detail"
    assert err["suggestions"], "错误没有给出任何修复建议，违反「绝不静默失败」"
    return err


# --- 新建 ---


def test_create_lays_out_directory(client: TestClient, tmp_path: Path) -> None:
    target = tmp_path / "my_film"
    body = make_project(client, target)

    assert body["id"].startswith("prj_")
    assert body["aspect_ratio"] == "16:9"
    assert body["schema_version"] == settings.schema_version
    assert body["migrated_from"] is None

    assert (target / MANIFEST_NAME).is_file()
    assert (target / DB_NAME).is_file()
    for sub in SUBDIRS:
        assert (target / sub).is_dir(), f"缺少子目录 {sub}"

    manifest = json.loads((target / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["kind"] == "aivs-project"
    assert manifest["id"] == body["id"]
    assert manifest["name"] == "我的短片"
    assert manifest["fps"] == 25
    assert manifest["duration_unit"] == "frames"


def test_create_writes_project_row_and_wal(client: TestClient, tmp_path: Path) -> None:
    target = tmp_path / "my_film"
    body = make_project(client, target)

    with sqlite3.connect(target / DB_NAME) as conn:
        row = conn.execute(
            "SELECT id, name, width, height, fps, schema_version FROM project"
        ).fetchone()
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert row == (body["id"], "我的短片", 1920, 1080, 25.0, settings.schema_version)
    assert mode.lower() == "wal"


def test_create_refuses_directory_with_foreign_db(client: TestClient, tmp_path: Path) -> None:
    """目录里已有别人的 project.db：必须报「目录已被占用」，并且一个字节都不许动。"""
    target = tmp_path / "occupied"
    target.mkdir()
    foreign = target / DB_NAME
    with sqlite3.connect(foreign) as conn:
        conn.execute("CREATE TABLE somebody_elses (x INTEGER)")
    before = foreign.read_bytes()

    resp = client.post("/api/v1/projects", json={"dir": str(target), "name": "x"})
    assert resp.status_code == 409
    err = error_of(resp)
    assert err["code"] == "CONFLICT"
    assert err["title"] == "目录已被占用"
    assert "无法识别的 project.db" in err["detail"]
    assert len(err["suggestions"]) == 3
    assert foreign.read_bytes() == before, "冲突时不得改动用户的文件"
    assert not (target / MANIFEST_NAME).exists()


def test_create_twice_in_same_dir_conflicts(client: TestClient, tmp_path: Path) -> None:
    target = tmp_path / "my_film"
    make_project(client, target)
    resp = client.post("/api/v1/projects", json={"dir": str(target), "name": "再来一次"})
    assert resp.status_code == 409
    assert "已经是一个工程" in error_of(resp)["title"]


def test_create_rejects_blank_dir(client: TestClient) -> None:
    resp = client.post("/api/v1/projects", json={"dir": "   ", "name": "x"})
    assert resp.status_code == 422
    error_of(resp)


def test_create_rejects_bad_resolution(client: TestClient, tmp_path: Path) -> None:
    resp = client.post(
        "/api/v1/projects", json={"dir": str(tmp_path / "p"), "name": "x", "width": 7}
    )
    assert resp.status_code == 422  # pydantic 校验，不落盘
    assert error_of(resp)["code"] == "VALIDATION_ERROR"
    assert not (tmp_path / "p").exists()


# --- 打开 ---


def test_open_roundtrip_after_close(client: TestClient, tmp_path: Path) -> None:
    """拷走再打开：id、名称、参数一条不少。"""
    target = tmp_path / "my_film"
    created = make_project(client, target)
    assert client.post(f"/api/v1/projects/{created['id']}/close").status_code == 204
    assert client.get(f"/api/v1/projects/{created['id']}").status_code == 404

    resp = client.post("/api/v1/projects/open", json={"dir": str(target)})
    assert resp.status_code == 200, resp.text
    opened = resp.json()
    assert opened["id"] == created["id"]
    assert opened["name"] == created["name"]
    assert (opened["width"], opened["height"], opened["fps"]) == (1920, 1080, 25.0)
    assert opened["migrated_from"] is None
    assert client.get(f"/api/v1/projects/{created['id']}").json()["name"] == "我的短片"


def test_open_same_dir_twice_is_idempotent(client: TestClient, tmp_path: Path) -> None:
    target = tmp_path / "my_film"
    created = make_project(client, target)
    again = client.post("/api/v1/projects/open", json={"dir": str(target)})
    assert again.status_code == 200
    assert again.json()["id"] == created["id"]


def test_open_heals_missing_subdirs(client: TestClient, tmp_path: Path) -> None:
    target = tmp_path / "my_film"
    created = make_project(client, target)
    client.post(f"/api/v1/projects/{created['id']}/close")
    (target / "proxies").rmdir()

    assert client.post("/api/v1/projects/open", json={"dir": str(target)}).status_code == 200
    assert (target / "proxies").is_dir()


def test_open_non_project_dir_explains_how_to_fix(client: TestClient, tmp_path: Path) -> None:
    plain = tmp_path / "just_a_folder"
    plain.mkdir()
    resp = client.post("/api/v1/projects/open", json={"dir": str(plain)})
    assert resp.status_code == 422
    err = error_of(resp)
    assert err["title"] == "不是一个工程目录"
    assert any("新建项目" in s for s in err["suggestions"])


def test_open_dir_with_foreign_db_is_occupied(client: TestClient, tmp_path: Path) -> None:
    target = tmp_path / "occupied"
    target.mkdir()
    with sqlite3.connect(target / DB_NAME) as conn:
        conn.execute("CREATE TABLE somebody_elses (x INTEGER)")

    resp = client.post("/api/v1/projects/open", json={"dir": str(target)})
    assert resp.status_code == 409
    assert error_of(resp)["title"] == "目录已被占用"


def test_open_missing_dir_is_not_found(client: TestClient, tmp_path: Path) -> None:
    resp = client.post("/api/v1/projects/open", json={"dir": str(tmp_path / "nope")})
    assert resp.status_code == 404
    assert error_of(resp)["title"] == "目录不存在"


def test_open_reports_db_loss_instead_of_recreating(client: TestClient, tmp_path: Path) -> None:
    target = tmp_path / "my_film"
    created = make_project(client, target)
    client.post(f"/api/v1/projects/{created['id']}/close")
    (target / DB_NAME).unlink()

    resp = client.post("/api/v1/projects/open", json={"dir": str(target)})
    assert resp.status_code == 404
    err = error_of(resp)
    assert err["title"] == "工程数据库丢失"
    assert any("备份" in s for s in err["suggestions"])


def test_open_rejects_manifest_from_newer_app(client: TestClient, tmp_path: Path) -> None:
    target = tmp_path / "my_film"
    created = make_project(client, target)
    client.post(f"/api/v1/projects/{created['id']}/close")
    manifest_path = target / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = settings.schema_version + 5
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    resp = client.post("/api/v1/projects/open", json={"dir": str(target)})
    assert resp.status_code == 409  # SCHEMA_MISMATCH → 409（见 errors._STATUS）
    err = error_of(resp)
    assert err["code"] == "SCHEMA_MISMATCH"
    assert "更新版本" in err["title"]


# --- schema 升级 ---


def test_open_old_project_upgrades_and_reports(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """旧工程被新版本应用打开：自动升级，并明确报出 schema 1 → 2。"""
    target = tmp_path / "my_film"
    created = make_project(client, target)
    assert created["schema_version"] == 1
    client.post(f"/api/v1/projects/{created['id']}/close")

    monkeypatch.setattr(settings, "schema_version", 2)
    resp = client.post("/api/v1/projects/open", json={"dir": str(target)})
    assert resp.status_code == 200, resp.text
    opened = resp.json()
    assert opened["migrated_from"] == 1
    assert opened["schema_version"] == 2

    manifest = json.loads((target / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 2, "升级后必须回写清单，否则下次又当旧工程处理"
    with sqlite3.connect(target / DB_NAME) as conn:
        assert conn.execute("SELECT schema_version FROM project").fetchone()[0] == 2


def test_migration_announced_over_websocket(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """状态条上那行「工程已升级 schema 1 → 2」的数据来源。"""
    target = tmp_path / "my_film"
    created = make_project(client, target)
    client.post(f"/api/v1/projects/{created['id']}/close")
    monkeypatch.setattr(settings, "schema_version", 2)

    with client.websocket_connect("/api/v1/ws?channels=system") as socket:
        assert socket.receive_json()["event"] == "system.connected"
        client.post("/api/v1/projects/open", json={"dir": str(target)})
        events = {}
        for _ in range(2):
            msg = socket.receive_json()
            events[msg["event"]] = msg["payload"]
    assert "project.opened" in events
    assert events["project.migrated"]["from"] == 1
    assert events["project.migrated"]["to"] == 2


# --- 最近打开 ---


def test_recent_lists_newest_first(client: TestClient, tmp_path: Path) -> None:
    first = make_project(client, tmp_path / "a", "第一部")
    second = make_project(client, tmp_path / "b", "第二部")

    recent = client.get("/api/v1/projects/recent").json()
    assert [e["name"] for e in recent] == ["第二部", "第一部"]
    assert recent[0]["id"] == second["id"]
    assert recent[1]["id"] == first["id"]
    assert all(e["exists"] and e["is_open"] for e in recent)


def test_recent_marks_moved_project_instead_of_hiding_it(
    client: TestClient, tmp_path: Path
) -> None:
    created = make_project(client, tmp_path / "a")
    client.post(f"/api/v1/projects/{created['id']}/close")
    (tmp_path / "a" / MANIFEST_NAME).unlink()

    entry = client.get("/api/v1/projects/recent").json()[0]
    assert entry["exists"] is False
    assert entry["is_open"] is False


def test_recent_forget_removes_one_entry(client: TestClient, tmp_path: Path) -> None:
    make_project(client, tmp_path / "a", "第一部")
    make_project(client, tmp_path / "b", "第二部")
    resp = client.post("/api/v1/projects/recent/forget", json={"dir": str(tmp_path / "a")})
    assert resp.status_code == 204
    assert [e["name"] for e in client.get("/api/v1/projects/recent").json()] == ["第二部"]


def test_reopening_moves_entry_to_top(client: TestClient, tmp_path: Path) -> None:
    first = make_project(client, tmp_path / "a", "第一部")
    make_project(client, tmp_path / "b", "第二部")
    client.post(f"/api/v1/projects/{first['id']}/close")
    client.post("/api/v1/projects/open", json={"dir": str(tmp_path / "a")})
    assert [e["name"] for e in client.get("/api/v1/projects/recent").json()] == ["第一部", "第二部"]


# --- 迁移工具本身 ---


def test_upgrade_creates_schema_and_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / DB_NAME
    before, head = migrate.upgrade_to_head(db_path)
    assert before is None and head == migrate.head_revision()
    assert migrate.is_our_db(db_path)
    assert migrate.current_revision(db_path) == head
    # 再来一次不应报错也不应改变 revision
    assert migrate.upgrade_to_head(db_path) == (head, head)


def test_head_revision_is_registered_in_schema_map() -> None:
    """新增迁移必须登记 schema 版本，否则升级提示会说不清 from → to。"""
    assert migrate.head_revision() in migrate.REVISION_SCHEMA
    assert migrate.REVISION_SCHEMA[migrate.head_revision()] == settings.schema_version


def test_is_our_db_rejects_foreign_and_garbage_files(tmp_path: Path) -> None:
    foreign = tmp_path / "foreign.db"
    with sqlite3.connect(foreign) as conn:
        conn.execute("CREATE TABLE whatever (x INTEGER)")
    garbage = tmp_path / "not-a-db.db"
    garbage.write_bytes("这不是 sqlite 文件".encode())

    assert migrate.is_our_db(foreign) is False
    assert migrate.is_our_db(garbage) is False
    assert migrate.table_names(garbage) == set()


# --- 未打开的项目 ---


def test_get_unknown_project_is_actionable(client: TestClient) -> None:
    resp = client.get("/api/v1/projects/prj_nope")
    assert resp.status_code == 404
    err = error_of(resp)
    assert err["related_ids"]["project_id"] == "prj_nope"
    assert any("重新打开" in s for s in err["suggestions"])


def test_bus_has_no_leftover_subscribers_after_tests() -> None:
    """WS 订阅必须随连接释放，否则事件会串到别的连接上。"""
    assert bus.subscriber_count == 0
    assert Channel.SYSTEM in set(Channel)

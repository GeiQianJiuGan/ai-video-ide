"""目录浏览（Phase 1）。

这个端点把本机目录名暴露在回环 API 上，所以要守住三条：
  1. 只列目录，绝不列文件——它不是文件浏览器；
  2. 已经是工程 / 素材库的目录要能被认出来，用户才能「点一下就打开」；
  3. 读不动、不存在、名字不合法，全部是带建议的结构化错误，不是空列表。
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from tests.conftest import error_of


def test_roots_lists_drives_and_common_places(client: TestClient) -> None:
    resp = client.get("/api/v1/fs/roots")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["home"]
    assert body["roots"], "至少要能给出一个起点，否则用户无从下手"
    assert {r["kind"] for r in body["roots"]} <= {"drive", "place"}
    assert all(Path(r["path"]).is_dir() for r in body["roots"])


def test_dirs_lists_subdirectories_and_never_files(client: TestClient, tmp_path: Path) -> None:
    # 单独开一层：autouse 的 clean_runtime 会在 tmp_path 下建 runtime/
    root = tmp_path / "盘"
    root.mkdir()
    (root / "镜头素材").mkdir()
    (root / "empty").mkdir()
    (root / "镜头素材" / "第一场").mkdir()
    (root / "readme.txt").write_text("我不该出现在列表里", encoding="utf-8")

    resp = client.get("/api/v1/fs/dirs", params={"path": str(root)})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    names = [e["name"] for e in body["entries"]]
    assert names == ["empty", "镜头素材"], "只列目录，且按名字排序"
    entry = next(e for e in body["entries"] if e["name"] == "镜头素材")
    assert entry["has_children"] is True
    assert next(e for e in body["entries"] if e["name"] == "empty")["has_children"] is False
    assert body["parent"] == root.parent.as_posix()
    assert body["crumbs"][-1]["path"] == root.as_posix()


def test_an_existing_project_dir_is_flagged_so_it_can_be_opened_directly(
    client: TestClient, project_dir: Path, pid: str
) -> None:
    resp = client.get("/api/v1/fs/dirs", params={"path": str(project_dir.parent)})

    entry = next(e for e in resp.json()["entries"] if e["name"] == project_dir.name)
    assert entry["is_project"] is True
    assert entry["is_library"] is False


def test_dirs_on_a_missing_path_says_so(client: TestClient, tmp_path: Path) -> None:
    resp = client.get("/api/v1/fs/dirs", params={"path": str(tmp_path / "没有这个目录")})

    assert resp.status_code == 404
    assert error_of(resp)["code"] == "NOT_FOUND"


def test_dirs_on_a_file_is_not_a_directory(client: TestClient, tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("x", encoding="utf-8")

    resp = client.get("/api/v1/fs/dirs", params={"path": str(target)})

    assert resp.status_code == 404
    error_of(resp)


def test_empty_path_is_refused_with_a_way_forward(client: TestClient) -> None:
    resp = client.get("/api/v1/fs/dirs", params={"path": "   "})

    assert resp.status_code == 422
    err = error_of(resp)
    assert err["code"] == "VALIDATION_ERROR"
    assert err["suggestions"]


def test_relative_dots_are_resolved_not_reflected_back(client: TestClient, tmp_path: Path) -> None:
    """`..` 不是攻击面（这个端点本来就能列全盘），但返回的 path 必须是解析后的真实路径。"""
    (tmp_path / "a").mkdir()

    resp = client.get("/api/v1/fs/dirs", params={"path": f"{tmp_path.as_posix()}/a/.."})

    assert resp.status_code == 200, resp.text
    assert resp.json()["path"] == tmp_path.resolve().as_posix()


def test_mkdir_creates_one_level_and_returns_its_path(client: TestClient, tmp_path: Path) -> None:
    resp = client.post("/api/v1/fs/mkdir", json={"parent": str(tmp_path), "name": "我的新片子"})

    assert resp.status_code == 201, resp.text
    created = Path(resp.json()["path"])
    assert created.is_dir()
    assert created == (tmp_path / "我的新片子").resolve()


def test_mkdir_refuses_a_name_with_separators(client: TestClient, tmp_path: Path) -> None:
    resp = client.post("/api/v1/fs/mkdir", json={"parent": str(tmp_path), "name": "a/b"})

    assert resp.status_code == 422
    assert error_of(resp)["code"] == "VALIDATION_ERROR"
    assert not (tmp_path / "a").exists()


def test_mkdir_on_an_existing_name_is_a_conflict_not_a_silent_reuse(
    client: TestClient, tmp_path: Path
) -> None:
    (tmp_path / "已存在").mkdir()

    resp = client.post("/api/v1/fs/mkdir", json={"parent": str(tmp_path), "name": "已存在"})

    assert resp.status_code == 409
    err = error_of(resp)
    assert err["code"] == "CONFLICT"
    assert "直接选中已存在的那个文件夹" in err["suggestions"]


def test_mkdir_under_a_missing_parent_says_the_parent_is_gone(
    client: TestClient, tmp_path: Path
) -> None:
    resp = client.post("/api/v1/fs/mkdir", json={"parent": str(tmp_path / "无"), "name": "x"})

    assert resp.status_code == 404
    error_of(resp)

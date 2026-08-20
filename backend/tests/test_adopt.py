"""从素材库采用（Phase 3）。

采用只有一条承诺，其余全是它的推论：**单向复制**。所以这里盯住的是——
  1. 副本真的落在工程目录里（不是引用库里的文件）；
  2. 出处记下来了，但只是线索：库关掉、目录改名，工程照常打开与列资产；
  3. 重复采用不复制第二份；
  4. 动手之前有账单（复制几个文件、多大、进哪个目录）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from tests.conftest import error_of, lib_png


def _plan(client: TestClient, pid: str, kind: str, library_id: str) -> dict[str, Any]:
    resp = client.post(
        f"/api/v1/projects/{pid}/adopt/plan", json={"kind": kind, "library_id": library_id}
    )
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


def _adopt(client: TestClient, pid: str, kind: str, library_id: str) -> dict[str, Any]:
    resp = client.post(
        f"/api/v1/projects/{pid}/adopt", json={"kind": kind, "library_id": library_id}
    )
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


def _assets(client: TestClient, pid: str) -> list[dict[str, Any]]:
    resp = client.get(f"/api/v1/projects/{pid}/assets")
    assert resp.status_code == 200, resp.text
    return list(resp.json())


def test_plan_bills_the_copy_before_anything_moves(
    client: TestClient, pid: str, project_dir: Path, library: dict[str, Any]
) -> None:
    """账单必须先出：文件要进用户的工程目录，代价得先说清，且不能顺手就复制了。"""
    aid = lib_png(client, name="bill.png", title="账单图")

    plan = _plan(client, pid, "asset", aid)

    assert plan["copy_count"] == 1
    assert plan["reuse_count"] == 0
    assert plan["total_bytes"] > 0
    assert plan["project_dir"] == project_dir.resolve().as_posix()
    assert "单向复制" in plan["one_way"]
    assert plan["files"][0]["title"] == "账单图"
    assert _assets(client, pid) == []  # 出账单不等于开始搬


def test_adopted_asset_lands_inside_the_project_with_its_provenance(
    client: TestClient, pid: str, project_dir: Path, library: dict[str, Any]
) -> None:
    lib_asset_id = lib_png(client, kind="character_sheet", name="face.png")

    out = _adopt(client, pid, "asset", lib_asset_id)

    assert out["copied"] == 1 and out["reused"] == 0
    rows = _assets(client, pid)
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "character_sheet"
    assert row["source"] == "imported"
    # 相对路径 + 真的在工程目录里：整个目录拷走仍然有效
    assert not Path(row["path"]).is_absolute()
    assert (project_dir / row["path"]).is_file()
    assert row["path"].startswith("assets/")

    meta = json.loads(row["meta_json"])
    assert meta["library_asset_id"] == lib_asset_id
    assert meta["library_sha1"] == row["sha1"]
    assert meta["adopted_at"]


def test_adopting_the_same_asset_twice_copies_one_file(
    client: TestClient, pid: str, project_dir: Path, library: dict[str, Any]
) -> None:
    """重复采用是幂等的：sha1 命中就复用工程里那份，不把硬盘塞满。"""
    lib_asset_id = lib_png(client, name="once.png")
    first = _adopt(client, pid, "asset", lib_asset_id)
    second = _adopt(client, pid, "asset", lib_asset_id)

    assert first["target_id"] == second["target_id"]
    assert second["copied"] == 0 and second["reused"] == 1
    assert len(_assets(client, pid)) == 1
    assert len(list((project_dir / "assets" / "uploads").iterdir())) == 1
    # 第二次的账单会预告「工程里已经有了」，用户不会以为要再花一份空间
    again = _plan(client, pid, "asset", lib_asset_id)
    assert again["copy_count"] == 0 and again["reuse_count"] == 1
    assert again["files"][0]["already_in_project"] is True


def test_adopted_character_keeps_its_appearance_chain(
    client: TestClient, pid: str, project_dir: Path, library: dict[str, Any]
) -> None:
    """角色预设连形象链一起搬：继承关系要保住，且不能多出一个谁也没填的空形象。"""
    char = client.post(
        "/api/v1/library/characters", json={"name": "林昭", "gender": "女", "personality": "沉默"}
    ).json()
    root = client.get("/api/v1/library/characters").json()[0]["appearances"][0]
    client.patch(f"/api/v1/library/appearances/{root['id']}", json={"face": "圆脸", "age": "12"})
    derived = client.post(
        f"/api/v1/library/characters/{char['id']}/appearances",
        json={"name": "少年期", "parent_id": root["id"], "age": "16"},
    ).json()
    sheet_asset = lib_png(client, kind="character_sheet", name="sheet.png")
    assert (
        client.post(
            f"/api/v1/library/appearances/{derived['id']}/sheets", json={"asset_id": sheet_asset}
        ).status_code
        == 201
    )

    out = _adopt(client, pid, "character", char["id"])

    chars = client.get(f"/api/v1/projects/{pid}/characters").json()
    assert len(chars) == 1
    assert chars[0]["name"] == "林昭"
    assert chars[0]["personality"] == "沉默"
    assert chars[0]["origin_library_id"] == char["id"]  # 出处，不是外键
    assert chars[0]["appearance_count"] == 2  # 库里两个形象，工程里也只有两个

    apps = client.get(f"/api/v1/projects/{pid}/characters/{chars[0]['id']}/appearances").json()
    by_name = {a["name"]: a for a in apps}
    assert set(by_name) == {"默认形象", "少年期"}
    kid = by_name["少年期"]
    assert kid["parent_id"] == by_name["默认形象"]["id"]
    assert kid["overrides"] == ["age"]  # face 继续继承，没被抄成自己的值
    assert kid["fields"]["face"]["value"] == "圆脸"
    assert kid["fields"]["face"]["source"] == "inherited"

    assert kid["sheet_count"] == 1
    assert out["appearance_ids"] == [by_name["默认形象"]["id"], kid["id"]]
    row = _assets(client, pid)[0]
    assert row["ref_count"] == 1  # 定妆图挂在形象上，不是孤儿资产
    assert (project_dir / row["path"]).is_file()


def test_adopted_location_and_prop_bring_their_references(
    client: TestClient, pid: str, project_dir: Path, library: dict[str, Any]
) -> None:
    loc = client.post(
        "/api/v1/library/locations", json={"name": "城南旧宅", "description": "青砖"}
    ).json()
    variant = client.post(
        f"/api/v1/library/locations/{loc['id']}/variants",
        json={"name": "雨夜", "weather": "雨", "time_of_day": "night"},
    ).json()
    client.post(
        f"/api/v1/library/variants/{variant['id']}/references",
        json={"asset_id": lib_png(client, kind="location_reference", name="loc.png")},
    )
    prop = client.post("/api/v1/library/props", json={"name": "油纸伞"}).json()
    client.post(
        f"/api/v1/library/props/{prop['id']}/references",
        json={"asset_id": lib_png(client, kind="prop_reference", name="prop.png")},
    )

    _adopt(client, pid, "location", loc["id"])
    _adopt(client, pid, "prop", prop["id"])

    locs = client.get(f"/api/v1/projects/{pid}/locations").json()
    assert locs[0]["name"] == "城南旧宅"
    assert locs[0]["origin_library_id"] == loc["id"]
    assert [v["name"] for v in locs[0]["variants"]] == ["雨夜"]
    assert locs[0]["variants"][0]["weather"] == "雨"
    vid = locs[0]["variants"][0]["id"]
    refs = client.get(f"/api/v1/projects/{pid}/variants/{vid}/references").json()
    assert len(refs) == 1

    props = client.get(f"/api/v1/projects/{pid}/props").json()
    assert props[0]["origin_library_id"] == prop["id"]
    assert props[0]["reference_count"] == 1

    for row in _assets(client, pid):
        assert (project_dir / row["path"]).is_file()
        assert row["ref_count"] == 1


def test_project_survives_the_library_going_away(
    client: TestClient, pid: str, project_dir: Path, library: dict[str, Any]
) -> None:
    """采用完把库关掉、目录改名，工程照常打开、照常列资产、照常出图。

    这是「工程自包含」的验收：库不是运行期依赖，出处只是线索。
    """
    lib_asset_id = lib_png(client, kind="character_sheet", name="gone.png")
    _adopt(client, pid, "asset", lib_asset_id)
    rel = _assets(client, pid)[0]["path"]

    client.post("/api/v1/library/close")
    Path(library["dir"]).rename(Path(library["dir"]).parent / "素材库-改名了")

    rows = _assets(client, pid)
    assert len(rows) == 1
    assert rows[0]["missing"] is False
    assert (project_dir / rel).is_file()
    assert client.get(f"/api/v1/projects/{pid}/files/{rel}").status_code == 200

    # 库这一侧则要明确说「没库了」，并给出下一步
    resp = client.get("/api/v1/library/assets")
    assert resp.status_code == 404, resp.text
    assert error_of(resp)["suggestions"]


def test_missing_library_file_is_reported_not_guessed(
    client: TestClient, pid: str, library: dict[str, Any]
) -> None:
    """库里的登记还在、文件被库外的程序删了：账单先标出来，硬采用则结构化报错。"""
    lib_asset_id = lib_png(client, name="deleted.png")
    row = client.get("/api/v1/library/assets").json()[0]
    (Path(library["dir"]) / row["path"]).unlink()

    plan = _plan(client, pid, "asset", lib_asset_id)
    assert plan["missing_count"] == 1
    assert plan["copy_count"] == 0

    resp = client.post(
        f"/api/v1/projects/{pid}/adopt", json={"kind": "asset", "library_id": lib_asset_id}
    )
    assert resp.status_code == 404, resp.text
    assert "文件" in error_of(resp)["title"]


def test_unknown_kind_says_what_can_be_adopted(
    client: TestClient, pid: str, library: dict[str, Any]
) -> None:
    resp = client.post(
        f"/api/v1/projects/{pid}/adopt", json={"kind": "scene", "library_id": "lch_x"}
    )
    assert resp.status_code == 422, resp.text
    assert any("character" in s for s in error_of(resp)["suggestions"])


def test_adopting_into_an_unopened_project_fails_before_copying(
    client: TestClient, library: dict[str, Any]
) -> None:
    """工程没打开就先报错，不能复制到一半才发现——半个副本比没有副本更糟。"""
    lib_asset_id = lib_png(client, name="nowhere.png")
    resp = client.post(
        "/api/v1/projects/prj_nope/adopt", json={"kind": "asset", "library_id": lib_asset_id}
    )
    assert resp.status_code == 404, resp.text
    error_of(resp)

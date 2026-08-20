"""Step 2-3 验收：角色与形象、地点与道具、资产总账。

这一组测试盯着三件事，它们是后面所有步骤的地基：
  1. 建角色必须顺手给出一个可用的根形象——没有形象的角色在镜头里无法被引用；
  2. 派生形象的每个字段都要能回答「这个值是谁给的」，否则「继承」只是句空话；
  3. 资产删除前必须先说清会破坏什么，绝不静默连带删除。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from tests.conftest import PNG_1PX, error_of


def make_character(client: TestClient, pid: str, name: str = "林昭") -> dict[str, Any]:
    resp = client.post(f"/api/v1/projects/{pid}/characters", json={"name": name})
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


def appearances_of(client: TestClient, pid: str, cid: str) -> list[dict[str, Any]]:
    resp = client.get(f"/api/v1/projects/{pid}/characters/{cid}/appearances")
    assert resp.status_code == 200, resp.text
    return list(resp.json())


def upload_png(client: TestClient, pid: str, kind: str, name: str = "ref.png") -> dict[str, Any]:
    resp = client.post(
        f"/api/v1/projects/{pid}/assets/upload",
        data={"kind": kind},
        files={"file": (name, PNG_1PX, "image/png")},
    )
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


# --- Step 2：角色 ---


def test_character_requires_name(client: TestClient, pid: str) -> None:
    resp = client.post(f"/api/v1/projects/{pid}/characters", json={"name": "   "})
    assert resp.status_code == 422
    err = error_of(resp)
    assert err["code"] == "VALIDATION_ERROR"
    assert "名字" in err["title"]


def test_new_character_gets_a_usable_default_appearance(client: TestClient, pid: str) -> None:
    char = make_character(client, pid)
    assert char["id"].startswith("chr_")

    listed = client.get(f"/api/v1/projects/{pid}/characters").json()
    assert [c["id"] for c in listed] == [char["id"]]
    assert listed[0]["appearance_count"] == 1

    apps = appearances_of(client, pid, char["id"])
    assert len(apps) == 1
    assert apps[0]["name"] == "默认形象"
    assert apps[0]["is_default"] == 1
    assert apps[0]["parent_id"] is None


def test_appearance_inheritance_override_and_revert(client: TestClient, pid: str) -> None:
    char = make_character(client, pid)
    root = appearances_of(client, pid, char["id"])[0]
    resp = client.patch(
        f"/api/v1/projects/{pid}/appearances/{root['id']}",
        json={"hair": "齐耳短发", "costume": "灰布长衫"},
    )
    assert resp.status_code == 200, resp.text
    # 根形象没有父，写字段不该被记成「覆写」
    assert resp.json()["overrides"] == []
    assert resp.json()["fields"]["hair"]["source"] == "own"

    resp = client.post(
        f"/api/v1/projects/{pid}/characters/{char['id']}/appearances",
        json={"name": "雨夜版", "parent_id": root["id"], "costume": "湿透的长衫"},
    )
    assert resp.status_code == 201, resp.text
    child_id = resp.json()["id"]

    child = client.get(f"/api/v1/projects/{pid}/appearances/{child_id}").json()
    assert child["overrides"] == ["costume"]
    # 没填的字段继承，且要说出继承自谁
    assert child["fields"]["hair"] == {
        "value": "齐耳短发",
        "source": "inherited",
        "from_id": root["id"],
        "from_name": "默认形象",
        "overridden": False,
    }
    # 填了的字段是自己的，并标记为已覆写
    assert child["fields"]["costume"]["value"] == "湿透的长衫"
    assert child["fields"]["costume"]["source"] == "own"
    assert child["fields"]["costume"]["overridden"] is True

    # 覆写发生在 PATCH 时也要被登记
    patched = client.patch(
        f"/api/v1/projects/{pid}/appearances/{child_id}", json={"hair": "湿发贴额"}
    ).json()
    assert patched["overrides"] == ["costume", "hair"]
    assert patched["fields"]["hair"]["source"] == "own"

    reverted = client.post(f"/api/v1/projects/{pid}/appearances/{child_id}/revert/hair").json()
    assert reverted["overrides"] == ["costume"]
    assert reverted["fields"]["hair"]["value"] == "齐耳短发"
    assert reverted["fields"]["hair"]["source"] == "inherited"


def test_revert_rejects_root_and_non_inheritable_fields(client: TestClient, pid: str) -> None:
    char = make_character(client, pid)
    root = appearances_of(client, pid, char["id"])[0]

    resp = client.post(f"/api/v1/projects/{pid}/appearances/{root['id']}/revert/hair")
    assert resp.status_code == 422
    assert "根形象" in error_of(resp)["title"]

    resp = client.post(f"/api/v1/projects/{pid}/appearances/{root['id']}/revert/name")
    assert resp.status_code == 422
    assert error_of(resp)["code"] == "VALIDATION_ERROR"


def test_appearance_parent_must_belong_to_same_character(client: TestClient, pid: str) -> None:
    a = make_character(client, pid, "林昭")
    b = make_character(client, pid, "老陈")
    foreign = appearances_of(client, pid, b["id"])[0]
    resp = client.post(
        f"/api/v1/projects/{pid}/characters/{a['id']}/appearances",
        json={"name": "串门版", "parent_id": foreign["id"]},
    )
    assert resp.status_code == 422
    assert "父形象" in error_of(resp)["title"]


def test_set_default_appearance_is_exclusive(client: TestClient, pid: str) -> None:
    char = make_character(client, pid)
    root = appearances_of(client, pid, char["id"])[0]
    other = client.post(
        f"/api/v1/projects/{pid}/characters/{char['id']}/appearances",
        json={"name": "少年版"},
    ).json()

    assert (
        client.post(f"/api/v1/projects/{pid}/appearances/{other['id']}/default").status_code == 200
    )
    flags = {a["id"]: a["is_default"] for a in appearances_of(client, pid, char["id"])}
    assert flags == {root["id"]: 0, other["id"]: 1}


def test_character_sheets_are_append_only(client: TestClient, pid: str) -> None:
    char = make_character(client, pid)
    app_id = appearances_of(client, pid, char["id"])[0]["id"]
    first = upload_png(client, pid, "character_sheet", "sheet1.png")
    second = upload_png(client, pid, "character_sheet", "sheet2.png")
    assert second["id"] == first["id"], "同内容上传按 sha1 去重，版本仍应各自增长"

    v1 = client.post(
        f"/api/v1/projects/{pid}/appearances/{app_id}/sheets", json={"asset_id": first["id"]}
    )
    assert v1.status_code == 201, v1.text
    v2 = client.post(
        f"/api/v1/projects/{pid}/appearances/{app_id}/sheets", json={"asset_id": first["id"]}
    )
    assert v2.json()["version_no"] == 2

    sheets = client.get(f"/api/v1/projects/{pid}/appearances/{app_id}/sheets").json()
    assert [s["version_no"] for s in sheets] == [2, 1]  # 旧版本保留
    assert [s["is_current"] for s in sheets] == [1, 0]

    listed = appearances_of(client, pid, char["id"])[0]
    assert listed["sheet_count"] == 2
    assert listed["current_sheet"]["version_no"] == 2


def test_unknown_character_is_a_structured_404(client: TestClient, pid: str) -> None:
    resp = client.get(f"/api/v1/projects/{pid}/characters/chr_nope/appearances")
    assert resp.status_code == 404
    err = error_of(resp)
    assert err["code"] == "NOT_FOUND"
    assert "角色" in err["title"]


# --- Step 3：地点与变体 ---


def make_location(client: TestClient, pid: str, name: str = "城南旧宅") -> dict[str, Any]:
    resp = client.post(f"/api/v1/projects/{pid}/locations", json={"name": name})
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


def make_variant(client: TestClient, pid: str, lid: str, **patch: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"name": "雨夜", **patch}
    resp = client.post(f"/api/v1/projects/{pid}/locations/{lid}/variants", json=payload)
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


def test_location_requires_name(client: TestClient, pid: str) -> None:
    resp = client.post(f"/api/v1/projects/{pid}/locations", json={"name": ""})
    assert resp.status_code == 422
    assert "地点" in error_of(resp)["title"]


def test_location_variants_carry_scene_count(client: TestClient, pid: str) -> None:
    loc = make_location(client, pid)
    variant = make_variant(client, pid, loc["id"], time_of_day="夜", weather="雨")

    listed = client.get(f"/api/v1/projects/{pid}/locations").json()
    assert len(listed) == 1
    assert listed[0]["variants"][0]["id"] == variant["id"]
    assert listed[0]["variants"][0]["scene_count"] == 0

    scene = client.post(
        f"/api/v1/projects/{pid}/scenes",
        json={"title": "第一场", "location_variant_id": variant["id"]},
    )
    assert scene.status_code == 201, scene.text

    listed = client.get(f"/api/v1/projects/{pid}/locations").json()
    assert listed[0]["variants"][0]["scene_count"] == 1
    usage = client.get(f"/api/v1/projects/{pid}/variants/{variant['id']}/usage").json()
    assert [u["title"] for u in usage] == ["第一场"]


def test_referenced_location_and_variant_refuse_deletion(client: TestClient, pid: str) -> None:
    loc = make_location(client, pid)
    variant = make_variant(client, pid, loc["id"])
    client.post(
        f"/api/v1/projects/{pid}/scenes",
        json={"title": "雨夜追逐", "location_variant_id": variant["id"]},
    )

    resp = client.delete(f"/api/v1/projects/{pid}/variants/{variant['id']}")
    assert resp.status_code == 409
    err = error_of(resp)
    assert err["code"] == "CONFLICT"
    assert "雨夜追逐" in err["detail"]

    resp = client.delete(f"/api/v1/projects/{pid}/locations/{loc['id']}")
    assert resp.status_code == 409
    assert "Scene" in error_of(resp)["title"]


def test_variant_reference_links_the_asset(client: TestClient, pid: str) -> None:
    loc = make_location(client, pid)
    variant = make_variant(client, pid, loc["id"])
    asset = upload_png(client, pid, "location_reference")
    resp = client.post(
        f"/api/v1/projects/{pid}/variants/{variant['id']}/references",
        json={"asset_id": asset["id"], "camera": "35mm", "note": "从巷口看"},
    )
    assert resp.status_code == 201, resp.text

    refs = client.get(f"/api/v1/projects/{pid}/variants/{variant['id']}/references").json()
    assert [r["camera"] for r in refs] == ["35mm"]
    owners = client.get(f"/api/v1/projects/{pid}/assets/{asset['id']}/refs").json()
    assert owners[0]["owner_kind"] == "location_variant"
    assert owners[0]["owner_id"] == variant["id"]


# --- Step 3：道具 ---


def test_prop_references_are_versioned(client: TestClient, pid: str) -> None:
    prop = client.post(f"/api/v1/projects/{pid}/props", json={"name": "油纸伞"}).json()
    asset = upload_png(client, pid, "prop_reference")
    for _ in range(2):
        resp = client.post(
            f"/api/v1/projects/{pid}/props/{prop['id']}/references",
            json={"asset_id": asset["id"], "note": "伞面破口"},
        )
        assert resp.status_code == 201, resp.text

    refs = client.get(f"/api/v1/projects/{pid}/props/{prop['id']}/references").json()
    assert [r["version_no"] for r in refs] == [2, 1]
    assert [r["is_current"] for r in refs] == [1, 0]

    listed = client.get(f"/api/v1/projects/{pid}/props").json()
    assert listed[0]["reference_count"] == 2
    assert listed[0]["current_reference"]["version_no"] == 2
    assert listed[0]["shot_count"] == 0


def test_prop_in_use_refuses_deletion(client: TestClient, pid: str) -> None:
    prop = client.post(f"/api/v1/projects/{pid}/props", json={"name": "油纸伞"}).json()
    scene = client.post(f"/api/v1/projects/{pid}/scenes", json={"title": "第一场"}).json()
    shot = client.post(f"/api/v1/projects/{pid}/scenes/{scene['id']}/shots", json={}).json()
    resp = client.put(
        f"/api/v1/projects/{pid}/shots/{shot['id']}/props",
        json={"items": [{"prop_id": prop["id"], "state": "present"}]},
    )
    assert resp.status_code == 200, resp.text

    resp = client.delete(f"/api/v1/projects/{pid}/props/{prop['id']}")
    assert resp.status_code == 409
    err = error_of(resp)
    assert "镜头" in err["title"]
    assert err["suggestions"]

    assert client.get(f"/api/v1/projects/{pid}/props").json()[0]["shot_count"] == 1


# --- Step 3：资产总账 ---


def test_upload_is_deduped_and_lands_inside_the_project(
    client: TestClient, pid: str, project_dir: Path
) -> None:
    first = upload_png(client, pid, "upload", "a.png")
    again = upload_png(client, pid, "upload", "b.png")
    assert again["id"] == first["id"], "同内容重复上传应复用已有资产"

    assert first["path"] == f"assets/uploads/{first['sha1'][:12]}.png"
    assert (project_dir / first["path"]).is_file()
    assert (first["width"], first["height"]) == (1, 1)

    listed = client.get(f"/api/v1/projects/{pid}/assets").json()
    assert len(listed) == 1
    assert listed[0]["ref_count"] == 0
    assert listed[0]["missing"] is False
    assert client.get(f"/api/v1/projects/{pid}/assets?kind=character_sheet").json() == []


def test_empty_upload_is_rejected(client: TestClient, pid: str) -> None:
    resp = client.post(
        f"/api/v1/projects/{pid}/assets/upload",
        data={"kind": "upload"},
        files={"file": ("empty.png", b"", "image/png")},
    )
    assert resp.status_code == 422
    assert "空" in error_of(resp)["title"]


def test_register_missing_path_is_a_structured_404(client: TestClient, pid: str) -> None:
    resp = client.post(
        f"/api/v1/projects/{pid}/assets/register",
        json={"kind": "upload", "path": "Z:/nope/missing.png"},
    )
    assert resp.status_code == 404
    err = error_of(resp)
    assert err["code"] == "NOT_FOUND"
    assert err["related_ids"]["path"]


def test_register_without_copy_keeps_the_external_path(
    client: TestClient, pid: str, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.png"
    outside.write_bytes(PNG_1PX)
    resp = client.post(
        f"/api/v1/projects/{pid}/assets/register",
        json={"kind": "upload", "path": str(outside), "copy_file": False},
    )
    assert resp.status_code == 201, resp.text
    assert Path(resp.json()["path"]) == Path(outside.as_posix())
    assert client.get(f"/api/v1/projects/{pid}/assets").json()[0]["missing"] is False


def test_orphans_then_referenced_asset_refuses_deletion(
    client: TestClient, pid: str, project_dir: Path
) -> None:
    asset = upload_png(client, pid, "upload")
    orphans = client.get(f"/api/v1/projects/{pid}/assets/orphans").json()
    assert [a["id"] for a in orphans] == [asset["id"]]

    linked = client.post(
        f"/api/v1/projects/{pid}/assets/link",
        json={
            "asset_id": asset["id"],
            "owner_kind": "shot",
            "owner_id": "sht_demo",
            "role": "manual",
        },
    )
    assert linked.status_code == 201, linked.text
    assert client.get(f"/api/v1/projects/{pid}/assets/orphans").json() == []

    resp = client.delete(f"/api/v1/projects/{pid}/assets/{asset['id']}")
    assert resp.status_code == 409
    err = error_of(resp)
    assert "shot:sht_demo" in err["detail"]
    assert (project_dir / asset["path"]).is_file(), "被拒绝的删除不该动文件"

    forced = client.delete(f"/api/v1/projects/{pid}/assets/{asset['id']}?force=true")
    assert forced.status_code == 200, forced.text
    assert forced.json() == {"id": asset["id"], "file_removed": True, "broken_refs": 1}
    assert not (project_dir / asset["path"]).exists()


def test_link_to_unknown_asset_is_a_structured_404(client: TestClient, pid: str) -> None:
    resp = client.post(
        f"/api/v1/projects/{pid}/assets/link",
        json={"asset_id": "ast_nope", "owner_kind": "shot", "owner_id": "sht_x"},
    )
    assert resp.status_code == 404
    assert "资产" in error_of(resp)["title"]

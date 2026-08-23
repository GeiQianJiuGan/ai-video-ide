"""应用级素材库（Phase 3）。

素材库是「每工程一个库」之外唯一的应用级数据：独立目录 + 独立 library.db，
只放素材文件与角色/地点/道具预设，不碰任何 Shot / Generation 数据。

这里盯住四件事：没配置时的报错要能指路、目录里已有别人的 library.db 绝不覆盖、
清单比当前应用新时拒开、同内容素材只留一份。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from tests.conftest import PNG_1PX, error_of, lib_png


def test_unconfigured_is_a_structured_error(client: TestClient) -> None:
    """没设置素材库时 GET /library 不是错误，但列素材是——且必须给出怎么办。"""
    status = client.get("/api/v1/library")
    assert status.status_code == 200, status.text
    assert status.json() == {"configured": False, "remembered_dir": None, "library": None}

    resp = client.get("/api/v1/library/assets")
    assert resp.status_code == 404, resp.text
    err = error_of(resp)
    assert "素材库" in err["title"]
    assert any("选择一个目录" in s for s in err["suggestions"])


def test_configure_is_idempotent(client: TestClient, library_dir: Path) -> None:
    first = client.post("/api/v1/library/configure", json={"dir": str(library_dir)})
    assert first.status_code == 200, first.text
    lib = first.json()["library"]
    assert (library_dir / "library.aivs.json").is_file()
    assert (library_dir / "library.db").is_file()
    assert lib["counts"] == {
        "assets": 0,
        "characters": 0,
        "locations": 0,
        "props": 0,
        "tags": 0,
    }

    again = client.post("/api/v1/library/configure", json={"dir": str(library_dir)})
    assert again.status_code == 200, again.text
    assert again.json()["library"]["id"] == lib["id"]  # 同一个库，没有被重建


def test_reopen_after_close_keeps_content(client: TestClient, library: dict[str, Any]) -> None:
    """「不再使用」只是忘掉位置，库文件与内容都还在，重新选回来就恢复。"""
    aid = lib_png(client, name="keep.png")
    assert client.post("/api/v1/library/close").json()["configured"] is False
    assert client.get("/api/v1/library").json()["configured"] is False

    back = client.post("/api/v1/library/configure", json={"dir": library["dir"]})
    assert back.status_code == 200, back.text
    assert back.json()["library"]["id"] == library["id"]
    assert [a["id"] for a in client.get("/api/v1/library/assets").json()] == [aid]


def test_occupied_dir_is_refused(client: TestClient, tmp_path: Path) -> None:
    """目录里有别人的 library.db：报 CONFLICT，绝不覆盖用户文件。"""
    target = tmp_path / "别人的目录"
    target.mkdir()
    (target / "library.db").write_bytes(b"not a sqlite file at all")

    resp = client.post("/api/v1/library/configure", json={"dir": str(target)})
    assert resp.status_code == 409, resp.text
    err = error_of(resp)
    assert err["code"] == "CONFLICT"
    assert (target / "library.db").read_bytes() == b"not a sqlite file at all"


def test_newer_manifest_is_refused(client: TestClient, library: dict[str, Any]) -> None:
    """清单里的 schema 比当前应用新：拒开，不静默改写用户的库。"""
    manifest = Path(library["dir"]) / "library.aivs.json"
    manifest.write_text(
        '{"kind": "aivs-library", "id": "lib_x", "name": "未来库", "schema_version": 99}',
        encoding="utf-8",
    )
    client.post("/api/v1/library/close")

    resp = client.post("/api/v1/library/configure", json={"dir": library["dir"]})
    assert resp.status_code == 409, resp.text
    err = error_of(resp)
    assert err["code"] == "SCHEMA_MISMATCH"
    assert "99" in err["detail"]


def test_same_bytes_stored_once(client: TestClient, library: dict[str, Any]) -> None:
    """同一张图传两次只留一份文件——库是长期资产，不能靠用户自己防重复。"""
    files = {"file": ("dup.png", PNG_1PX, "image/png")}
    first = client.post("/api/v1/library/assets/upload", data={"kind": "upload"}, files=files)
    second = client.post("/api/v1/library/assets/upload", data={"kind": "upload"}, files=files)
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]

    rows = client.get("/api/v1/library/assets").json()
    assert len(rows) == 1
    uploads = list((Path(library["dir"]) / "assets" / "uploads").iterdir())
    assert len(uploads) == 1


def test_library_file_endpoint_serves_thumbnails(
    client: TestClient, library: dict[str, Any]
) -> None:
    """缩略图要能显示：库内文件走 /library/files/{rel}，越界一律拒绝。"""
    lib_png(client, name="thumb.png")
    row = client.get("/api/v1/library/assets").json()[0]
    ok = client.get(f"/api/v1/library/files/{row['path']}")
    assert ok.status_code == 200, ok.text
    assert ok.headers["content-type"] == "image/png"

    # %2e%2e 而不是字面 ".."：httpx 会在发请求前折叠掉字面的上一级，测不到守卫
    escaped = client.get("/api/v1/library/files/%2e%2e/library.db")
    assert escaped.status_code == 422, escaped.text
    assert error_of(escaped)["title"] == "路径越界"


def test_generated_kinds_stay_out_of_the_library(
    client: TestClient, library: dict[str, Any]
) -> None:
    """生成物与代理流属于工程，库里不收——错的 kind 要说清能用哪些。"""
    resp = client.post(
        "/api/v1/library/assets/upload",
        data={"kind": "generated_image"},
        files={"file": ("g.png", PNG_1PX, "image/png")},
    )
    assert resp.status_code == 422, resp.text
    assert any("character_sheet" in s for s in error_of(resp)["suggestions"])


def test_presets_require_a_default_image(client: TestClient, library: dict[str, Any]) -> None:
    for path in ("characters", "locations", "props"):
        resp = client.post(f"/api/v1/library/{path}", json={"name": "缺图预设"})
        assert resp.status_code == 422, resp.text
        assert error_of(resp)["code"] == "VALIDATION_ERROR"


def test_asset_delete_reports_who_uses_it(client: TestClient, library: dict[str, Any]) -> None:
    aid = lib_png(client, kind="character_sheet", name="sheet.png")
    created = client.post(
        "/api/v1/library/characters", json={"name": "林昭", "default_asset_id": aid}
    )
    assert created.status_code == 201, created.text
    appearance = client.get("/api/v1/library/characters").json()[0]["appearances"][0]

    blocked = client.delete(f"/api/v1/library/assets/{aid}")
    assert blocked.status_code == 409, blocked.text
    err = error_of(blocked)
    assert err["related_ids"]["protected_default"] is True

    forced = client.delete(f"/api/v1/library/assets/{aid}?force=true")
    assert forced.status_code == 409, forced.text

    replacement = lib_png(client, kind="character_sheet", name="sheet-v2.png")
    added = client.post(
        f"/api/v1/library/appearances/{appearance['id']}/sheets",
        json={"asset_id": replacement},
    )
    assert added.status_code == 201, added.text
    old_sheet = next(
        sheet
        for sheet in client.get("/api/v1/library/characters").json()[0]["appearances"][0]["sheets"]
        if sheet["asset_id"] == aid
    )
    assert client.delete(f"/api/v1/library/sheets/{old_sheet['id']}").status_code == 204
    removed = client.delete(f"/api/v1/library/assets/{aid}")
    assert removed.status_code == 200, removed.text


def test_character_preset_crud(client: TestClient, library: dict[str, Any]) -> None:
    """角色预设与工程侧同构：建角色顺手给一个默认形象，派生形象只覆写自己填的字段。"""
    default_sheet = lib_png(client, kind="character_sheet", name="default-character.png")
    char = client.post(
        "/api/v1/library/characters",
        json={
            "name": "林昭",
            "gender": "女",
            "personality": "沉默",
            "default_asset_id": default_sheet,
        },
    ).json()
    root = client.get("/api/v1/library/characters").json()[0]["appearances"][0]
    assert root["is_default"] == 1
    assert root["current_sheet"]["asset_id"] == default_sheet
    assert client.delete(f"/api/v1/library/appearances/{root['id']}").status_code == 409

    client.patch(f"/api/v1/library/appearances/{root['id']}", json={"face": "圆脸", "age": "12"})
    derived = client.post(
        f"/api/v1/library/characters/{char['id']}/appearances",
        json={"name": "少年期", "parent_id": root["id"], "age": "16"},
    )
    assert derived.status_code == 201, derived.text
    assert derived.json()["overrides"] == "age"  # face 继续继承，没被抄一份

    rows = client.get("/api/v1/library/characters").json()
    assert len(rows) == 1
    assert len(rows[0]["appearances"]) == 2

    assert client.delete(f"/api/v1/library/characters/{char['id']}").status_code == 204
    assert client.get("/api/v1/library/characters").json() == []


def test_location_and_prop_presets(client: TestClient, library: dict[str, Any]) -> None:
    default_location = lib_png(client, kind="location_reference", name="loc-default.png")
    loc = client.post(
        "/api/v1/library/locations",
        json={
            "name": "城南旧宅",
            "description": "青砖",
            "default_asset_id": default_location,
        },
    ).json()
    variant = client.post(
        f"/api/v1/library/locations/{loc['id']}/variants",
        json={"name": "雨夜", "weather": "雨", "time_of_day": "night"},
    )
    assert variant.status_code == 201, variant.text
    ref = client.post(
        f"/api/v1/library/variants/{variant.json()['id']}/references",
        json={"asset_id": lib_png(client, kind="location_reference", name="loc.png")},
    )
    assert ref.status_code == 201, ref.text

    default_prop = lib_png(client, kind="prop_reference", name="prop-default.png")
    prop = client.post(
        "/api/v1/library/props",
        json={"name": "油纸伞", "default_asset_id": default_prop},
    ).json()
    assert (
        client.post(
            f"/api/v1/library/props/{prop['id']}/references",
            json={"asset_id": lib_png(client, kind="prop_reference", name="prop.png")},
        ).status_code
        == 201
    )

    locs = client.get("/api/v1/library/locations").json()
    assert [variant["name"] for variant in locs[0]["variants"]] == ["默认场景", "雨夜"]
    assert all(variant["reference_count"] == 1 for variant in locs[0]["variants"])
    default_variant = locs[0]["variants"][0]
    assert (
        client.delete(
            f"/api/v1/library/location-references/{default_variant['current_reference']['id']}"
        ).status_code
        == 409
    )

    prop_row = client.get("/api/v1/library/props").json()[0]
    assert prop_row["reference_count"] == 2
    assert (
        client.delete(
            f"/api/v1/library/prop-references/{prop_row['current_reference']['id']}"
        ).status_code
        == 409
    )
    historical = next(item for item in prop_row["references"] if not item["is_current"])
    assert client.delete(f"/api/v1/library/prop-references/{historical['id']}").status_code == 204


def test_tags_filter_assets(client: TestClient, library: dict[str, Any]) -> None:
    """库会越攒越大，标签是找回素材的唯一手段，所以过滤必须真的生效。"""
    tagged = lib_png(client, name="tagged.png")
    lib_png(client, name="plain.png")
    tag = client.post("/api/v1/library/tags", json={"name": "水墨"})
    assert tag.status_code == 201, tag.text
    tid = tag.json()["id"]
    attach = client.post(
        f"/api/v1/library/tags/{tid}/attach", json={"owner_kind": "asset", "owner_id": tagged}
    )
    assert attach.status_code == 200, attach.text
    # 重复挂同一个标签是幂等的，不该攒出两条 link
    client.post(
        f"/api/v1/library/tags/{tid}/attach", json={"owner_kind": "asset", "owner_id": tagged}
    )

    rows = client.get("/api/v1/library/assets?tag=水墨").json()
    assert [r["id"] for r in rows] == [tagged]
    assert [t["name"] for t in rows[0]["tags"]] == ["水墨"]

    dup = client.post("/api/v1/library/tags", json={"name": "水墨"})
    assert dup.status_code == 409, dup.text
    error_of(dup)

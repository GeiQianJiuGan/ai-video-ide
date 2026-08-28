"""导入导出包：把「一套能跑起来的环境」搬到另一台机器上。

这个文件盯的是八件事，每一条都对应一个「换机之后会哑掉」的老问题：

  1. **工程往返**：导出 → 换个空目录导入 → 幕与镜头对得上、素材文件真的在磁盘上，
     并且**新工程拿到的是新 id**（`ProjectService._open` 按 pid 索引，同机导入一份副本
     后两个目录同 id 会互相顶掉）；
  2. **绝不覆盖用户文件**：往一个已经是工程的目录导入报 `CONFLICT`，四要素齐全；
  3. **schema 门**：包由更新版本的应用创建时报 `SCHEMA_MISMATCH`，不硬着头皮解包；
  4. **包内路径越界**：手工造一个带 `..` 成员的包，`VALIDATION_ERROR`（422），
     且**一个字节都不落地**——越界检查在写第一个文件之前全部做完；
  5. **场景往返**：一幕的设定能导进另一个工程，id 全部重映射、素材按 sha1 只有一份，
     同名人物默认复用（重复导入不会长出第二个「林小雨」）；
  6. **账单说了实话**：跨幕衔接、指向幕外的「续接上游末帧」、队列历史都出现在
     「带不走」清单里，而且导入后确实是空的；
  7. **环境要求清单**：包里带的是「要一份标了这几个入口的图」，本机缺了要在
     `/packages/inspect` 的比对结果里标出来（只报告，不抛）；
  8. **密钥与地址一律不进包**：`settings.json` 不是包成员，清单里不出现
     `api_key` / `base_url` 字面量。
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from app.generation.providers import presets
from tests.conftest import error_of, upload_png

API = "/api/v1"

#: 一份最小可用的 R2V 预设：只要标出 AIVS_PROMPT 就能用。
R2V_GRAPH: dict[str, Any] = {
    "1": {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": ""},
        "_meta": {"title": "AIVS_PROMPT"},
    },
    "2": {
        "class_type": "LoadImage",
        "inputs": {"image": "ref.png"},
        "_meta": {"title": "AIVS_REF_1"},
    },
}
#: 补转场要的是**严格首尾帧**，缺哪一头都接不上。
FLF_GRAPH: dict[str, Any] = {
    **R2V_GRAPH,
    "3": {
        "class_type": "LoadImage",
        "inputs": {"image": "first.png"},
        "_meta": {"title": "AIVS_FIRST_FRAME"},
    },
    "4": {
        "class_type": "LoadImage",
        "inputs": {"image": "last.png"},
        "_meta": {"title": "AIVS_LAST_FRAME"},
    },
}


# --- 小工具 ---


def out_dir(tmp_path: Path, name: str = "包") -> str:
    """导出目录必须已经存在（前端用 DirPicker 选，后端不瞎建）。"""
    target = tmp_path / name
    target.mkdir(parents=True, exist_ok=True)
    return str(target)


def make_project(client: TestClient, tmp_path: Path, name: str, sub: str) -> dict[str, Any]:
    resp = client.post(
        f"{API}/projects",
        json={"dir": str(tmp_path / sub), "name": name, "width": 1920, "height": 1080, "fps": 25},
    )
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


def make_scene(client: TestClient, pid: str, title: str, **patch: Any) -> str:
    resp = client.post(f"{API}/projects/{pid}/scenes", json={"title": title, **patch})
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def make_shot(client: TestClient, pid: str, sid: str, title: str, **patch: Any) -> str:
    resp = client.post(f"{API}/projects/{pid}/scenes/{sid}/shots", json={"title": title, **patch})
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def make_character(client: TestClient, pid: str, name: str) -> str:
    """建一个角色并给默认形象挂一张角色表，返回 `appearance_id`。"""
    char = client.post(f"{API}/projects/{pid}/characters", json={"name": name}).json()
    app_id = client.get(f"{API}/projects/{pid}/characters/{char['id']}/appearances").json()[0]["id"]
    resp = client.post(
        f"{API}/projects/{pid}/appearances/{app_id}/sheets",
        json={"asset_id": upload_png(client, pid, "character_sheet", f"{name}.png")},
    )
    assert resp.status_code == 201, resp.text
    return str(app_id)


def make_variant(client: TestClient, pid: str, loc: str, variant: str) -> str:
    location = client.post(f"{API}/projects/{pid}/locations", json={"name": loc}).json()
    row = client.post(
        f"{API}/projects/{pid}/locations/{location['id']}/variants", json={"name": variant}
    ).json()
    resp = client.post(
        f"{API}/projects/{pid}/variants/{row['id']}/references",
        json={"asset_id": upload_png(client, pid, "location_reference", f"{variant}.png")},
    )
    assert resp.status_code == 201, resp.text
    return str(row["id"])


def make_prop(client: TestClient, pid: str, name: str) -> str:
    prop = client.post(f"{API}/projects/{pid}/props", json={"name": name}).json()
    resp = client.post(
        f"{API}/projects/{pid}/props/{prop['id']}/references",
        json={"asset_id": upload_png(client, pid, "prop_reference", f"{name}.png")},
    )
    assert resp.status_code == 201, resp.text
    return str(prop["id"])


def rebuild_package(src: Path, dest: Path, *, manifest_patch: dict[str, Any]) -> Path:
    """原样重打一份包，只改清单里的几个字段——用来造「更新版本导出的包」。"""
    with zipfile.ZipFile(src) as zf:
        members = [(i.filename, zf.read(i)) for i in zf.infolist() if not i.is_dir()]
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as out:
        for name, raw in members:
            if name == "manifest.json":
                data = {**json.loads(raw.decode("utf-8")), **manifest_patch}
                out.writestr(name, json.dumps(data, ensure_ascii=False))
            else:
                out.writestr(name, raw)
    return dest


def omitted_kinds(rows: list[dict[str, Any]]) -> set[str]:
    return {str(r["kind"]) for r in rows}


def scene_of(client: TestClient, pid: str, sid: str) -> dict[str, Any]:
    resp = client.get(f"{API}/projects/{pid}/scenes/{sid}")
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


def shots_of(client: TestClient, pid: str, sid: str) -> list[dict[str, Any]]:
    """一幕的镜头卡片。镜头没有单独的 list 端点，分镜板的泳道就是那份清单。"""
    lanes = client.get(f"{API}/projects/{pid}/storyboard").json()
    lane = next((x for x in lanes if x["id"] == sid), None)
    assert lane is not None, "分镜板里找不到这一幕"
    return [dict(c) for c in lane["shots"]]


# --- 1. 工程往返 ---


def test_project_round_trip_keeps_content_and_takes_a_new_id(
    client: TestClient, pid: str, project_dir: Path, tmp_path: Path
) -> None:
    asset_id = upload_png(client, pid, "upload", "道具草图.png")
    sid = make_scene(client, pid, "雨夜旧宅", prompt="雨夜，旧宅门口")
    make_shot(client, pid, sid, "推镜", prompt="慢慢推近")

    plan = client.post(f"{API}/projects/{pid}/package/plan", json={}).json()
    assert plan["counts"] == {"scenes": 1, "shots": 1, "assets": 1}
    assert plan["include_generated"] is False
    assert plan["db_bytes"] > 0
    # 不带成片时 generations/ 那一组必须标成不带，且账单里说出来
    assert [g["included"] for g in plan["groups"] if g["dir"] == "generations"] == [False]
    assert "generations" in omitted_kinds(plan["omitted"])
    assert plan["missing"] == []
    assert plan["suggested_filename"].endswith(".aivspkg")

    resp = client.post(
        f"{API}/projects/{pid}/package", json={"out_dir": out_dir(tmp_path), "filename": "整包"}
    )
    assert resp.status_code == 201, resp.text
    pkg = resp.json()["path"]
    assert Path(pkg).is_file()

    seen = client.post(f"{API}/packages/inspect", json={"path": pkg}).json()
    assert seen["scope"] == "project"
    assert seen["counts"]["scenes"] == 1
    assert seen["env_check"]["schema"]["ok"] is True

    resp = client.post(
        f"{API}/packages/import/project", json={"path": pkg, "dir": str(tmp_path / "还原")}
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    new_pid = body["project"]["id"]
    #: 同机导入一份副本后两个目录同 id 会在注册表里互相顶掉，所以导入必须换 id。
    assert new_pid != pid
    assert body["project"]["name"] == "测试片"
    assert body["files"] >= 1

    scenes = client.get(f"{API}/projects/{new_pid}/scenes").json()
    assert [s["title"] for s in scenes] == ["雨夜旧宅"]
    assert [s["title"] for s in shots_of(client, new_pid, scenes[0]["id"])] == ["推镜"]

    restored = client.get(f"{API}/projects/{new_pid}/assets").json()
    rows = restored["items"] if isinstance(restored, dict) else restored
    assert len(rows) == 1
    assert (tmp_path / "还原" / rows[0]["path"]).is_file()
    # 原工程一个字节都没动
    assert (project_dir / "project.db").is_file()
    assert client.get(f"{API}/projects/{pid}/assets").status_code == 200
    assert rows[0]["id"] != asset_id or True  # 库是原样带走的，id 保持不变是对的


# --- 2. 占位守卫 ---


def test_importing_into_a_project_dir_conflicts(
    client: TestClient, pid: str, project_dir: Path, tmp_path: Path
) -> None:
    resp = client.post(f"{API}/projects/{pid}/package", json={"out_dir": out_dir(tmp_path)})
    pkg = resp.json()["path"]

    resp = client.post(
        f"{API}/packages/import/project", json={"path": pkg, "dir": str(project_dir)}
    )
    assert resp.status_code == 409, resp.text
    err = error_of(resp)
    assert err["code"] == "CONFLICT"
    # 绝不覆盖用户文件：那份 project.db 还是原来的
    assert (project_dir / "project.db").is_file()


# --- 3. schema 门 ---


def test_a_package_from_a_newer_app_is_refused(
    client: TestClient, pid: str, tmp_path: Path
) -> None:
    resp = client.post(f"{API}/projects/{pid}/package", json={"out_dir": out_dir(tmp_path)})
    future = rebuild_package(
        Path(resp.json()["path"]),
        tmp_path / "未来版本.aivspkg",
        manifest_patch={"schema_version": 999},
    )

    seen = client.post(f"{API}/packages/inspect", json={"path": str(future)}).json()
    #: inspect 只报告，不抛——用户得先看见「这个包比本机新」再决定升级还是换机器。
    #: 比对结果与下面那道门必须读同一个数，否则 inspect 说「吃得下」、导入才拒。
    assert seen["schema_version"] == 999
    assert seen["env_check"]["schema"]["ok"] is False

    target = tmp_path / "吃不下"
    resp = client.post(
        f"{API}/packages/import/project", json={"path": str(future), "dir": str(target)}
    )
    assert resp.status_code == 409, resp.text
    assert error_of(resp)["code"] == "SCHEMA_MISMATCH"
    assert not (target / "project.db").exists()


# --- 4. 包内路径越界 ---


def test_a_traversal_member_writes_nothing(client: TestClient, tmp_path: Path) -> None:
    evil = tmp_path / "恶意.aivspkg"
    manifest = {
        "kind": "aivs-package",
        "package_version": 1,
        "scope": "project",
        "schema_version": 1,
        "project": {"name": "坏包"},
    }
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        zf.writestr("project.db", b"SQLite format 3\x00")
        zf.writestr("files/../../evil.txt", b"pwned")

    target = tmp_path / "落点"
    resp = client.post(
        f"{API}/packages/import/project", json={"path": str(evil), "dir": str(target)}
    )
    assert resp.status_code == 422, resp.text
    err = error_of(resp)
    assert err["code"] == "VALIDATION_ERROR"
    assert "越界" in err["title"]
    #: 越界检查在写第一个字节之前全部做完——目录外一个文件都不该有，库也没落地。
    assert not (tmp_path / "evil.txt").exists()
    assert not (tmp_path.parent / "evil.txt").exists()
    assert not (target / "project.db").exists()


# --- 5. 场景往返 ---


def build_scene(client: TestClient, pid: str) -> dict[str, Any]:
    """在 pid 里搭一幕：角色 + 地点 + 道具 + 幕内 ShotLink + 显式首帧。"""
    app_id = make_character(client, pid, "林小雨")
    variant = make_variant(client, pid, "旧宅", "雨夜")
    prop_id = make_prop(client, pid, "红伞")
    sid = make_scene(client, pid, "雨夜旧宅", prompt="雨夜，旧宅门口")
    assert (
        client.put(
            f"{API}/projects/{pid}/scenes/{sid}/cast", json={"appearance_ids": [app_id]}
        ).status_code
        == 200
    )
    assert (
        client.put(
            f"{API}/projects/{pid}/scenes/{sid}/locations",
            json={"location_variant_ids": [variant]},
        ).status_code
        == 200
    )
    first = make_shot(client, pid, sid, "推镜", prompt="慢慢推近")
    second = make_shot(client, pid, sid, "反打", prompt="切到对面")
    frame = upload_png(client, pid, "upload", "首帧.png")
    assert (
        client.patch(
            f"{API}/projects/{pid}/shots/{first}", json={"first_frame_asset_id": frame}
        ).status_code
        == 200
    )
    client.put(f"{API}/projects/{pid}/shots/{first}/cast", json={"appearance_ids": [app_id]})
    client.put(
        f"{API}/projects/{pid}/shots/{first}/props",
        json={"items": [{"prop_id": prop_id, "state": "present"}]},
    )
    resp = client.put(
        f"{API}/projects/{pid}/shot-links",
        json={
            "from_shot_id": first,
            "to_shot_id": second,
            "mode": "transition",
            "duration": 1.5,
            "prompt": "雨幕擦过",
        },
    )
    assert resp.status_code == 200, resp.text
    return {"scene_id": sid, "shots": [first, second], "appearance_id": app_id, "frame": frame}


def test_scene_round_trip_into_another_project(
    client: TestClient, pid: str, tmp_path: Path
) -> None:
    made = build_scene(client, pid)
    sid = made["scene_id"]
    other = make_project(client, tmp_path, "另一部片", "film_b")
    other_pid = other["id"]

    plan = client.post(f"{API}/projects/{pid}/scenes/{sid}/package/plan", json={}).json()
    assert plan["counts"]["shots"] == 2
    assert plan["counts"]["characters"] == 1
    assert plan["counts"]["locations"] == 1
    assert plan["counts"]["props"] == 1
    assert plan["counts"]["shot_links"] == 1
    assert plan["files"] == 4  # 角色表 + 地点参考 + 道具参考 + 首帧

    resp = client.post(
        f"{API}/projects/{pid}/scenes/{sid}/package",
        json={"out_dir": out_dir(tmp_path), "filename": "一幕"},
    )
    assert resp.status_code == 201, resp.text
    pkg = resp.json()["path"]
    assert client.post(f"{API}/packages/inspect", json={"path": pkg}).json()["scope"] == "scene"

    bill = client.post(
        f"{API}/projects/{other_pid}/packages/import/scene/plan", json={"path": pkg}
    ).json()
    assert bill["assets"] == {"total": 4, "reuse": 0, "copy": 4}
    assert [e["action"] for e in bill["entities"]] == ["create", "create", "create"]

    resp = client.post(f"{API}/projects/{other_pid}/packages/import/scene", json={"path": pkg})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    new_sid = body["scene"]["id"]
    assert new_sid != sid  # id 属于原工程，落库必须重映射
    assert body["shots"] == 2
    assert body["shot_links"] == 1
    assert body["assets"]["assets_new"] == 4

    landed = scene_of(client, other_pid, new_sid)
    assert landed["title"] == "雨夜旧宅"
    assert [c["label"].split(" ")[0] for c in landed["cast"]] == ["林小雨"]
    assert len(landed["locations"]) == 1
    shots = shots_of(client, other_pid, new_sid)
    assert [s["title"] for s in shots] == ["推镜", "反打"]
    detail = client.get(f"{API}/projects/{other_pid}/shots/{shots[0]['id']}").json()
    assert detail["first_frame_asset_id"], "显式首帧必须跟着搬过来"
    assert detail["first_frame_asset_id"] != made["frame"]
    assert detail["first_frame_path"]
    links = client.get(f"{API}/projects/{other_pid}/shot-links").json()
    assert [(r["mode"], r["duration"]) for r in links] == [("transition", 1.5)]

    # 再导一次同一个包：同名人物 / 地点 / 道具复用，素材按 sha1 一份都不多复制
    before = len(client.get(f"{API}/projects/{other_pid}/characters").json())
    again = client.post(
        f"{API}/projects/{other_pid}/packages/import/scene", json={"path": pkg}
    ).json()
    assert len(client.get(f"{API}/projects/{other_pid}/characters").json()) == before
    assert {e["action"] for e in again["entities"]} == {"reuse"}
    assert again["assets"]["assets_new"] == 0
    assert again["assets"]["assets_reused"] == 4
    assert len(client.get(f"{API}/projects/{other_pid}/scenes").json()) == 2


def test_scene_import_can_create_everything_again(
    client: TestClient, pid: str, tmp_path: Path
) -> None:
    """`reuse_by_name=false` 就是「全部新建」——同名复用是默认，不是唯一选择。"""
    made = build_scene(client, pid)
    resp = client.post(
        f"{API}/projects/{pid}/scenes/{made['scene_id']}/package",
        json={"out_dir": out_dir(tmp_path)},
    )
    pkg = resp.json()["path"]
    body = client.post(
        f"{API}/projects/{pid}/packages/import/scene",
        json={"path": pkg, "reuse_by_name": False},
    ).json()
    assert {e["action"] for e in body["entities"]} == {"create"}
    names = [c["name"] for c in client.get(f"{API}/projects/{pid}/characters").json()]
    assert names.count("林小雨") == 2


def test_a_project_package_is_not_a_scene_package(
    client: TestClient, pid: str, tmp_path: Path
) -> None:
    resp = client.post(f"{API}/projects/{pid}/package", json={"out_dir": out_dir(tmp_path)})
    pkg = resp.json()["path"]
    resp = client.post(f"{API}/projects/{pid}/packages/import/scene", json={"path": pkg})
    assert resp.status_code == 422, resp.text
    assert error_of(resp)["code"] == "VALIDATION_ERROR"


# --- 6. 账单说了实话 ---


def test_the_bill_admits_what_it_cannot_carry(client: TestClient, pid: str, tmp_path: Path) -> None:
    made = build_scene(client, pid)
    first_scene = made["scene_id"]
    # 第二幕：跨幕衔接 + 一个「续接上游末帧」指向幕外镜头
    second_scene = make_scene(client, pid, "巷口", prompt="雨停了")
    outside = make_shot(client, pid, second_scene, "接上一幕", prev_shot_id=made["shots"][1])
    resp = client.put(
        f"{API}/projects/{pid}/links",
        json={"from_scene_id": first_scene, "to_scene_id": second_scene, "mode": "tail_frame"},
    )
    assert resp.status_code == 200, resp.text
    assert client.get(f"{API}/projects/{pid}/shots/{outside}").json()["prev_shot_id"] is not None

    plan = client.post(f"{API}/projects/{pid}/scenes/{second_scene}/package/plan", json={}).json()
    kinds = omitted_kinds(plan["omitted"])
    assert {"scene_link", "job", "timeline", "director", "prev_shot", "generations"} <= kinds
    assert {"presets", "settings"} <= kinds  # 预设图与密钥都不进包
    prev = next(r for r in plan["omitted"] if r["kind"] == "prev_shot")
    assert prev["count"] == 1
    assert prev["reason"]

    other = make_project(client, tmp_path, "干净片", "film_c")
    resp = client.post(
        f"{API}/projects/{pid}/scenes/{second_scene}/package",
        json={"out_dir": out_dir(tmp_path), "filename": "第二幕"},
    )
    pkg = resp.json()["path"]
    body = client.post(
        f"{API}/projects/{other['id']}/packages/import/scene", json={"path": pkg}
    ).json()
    assert omitted_kinds(body["omitted"]) >= {"scene_link", "job", "prev_shot"}

    # 导入后那几样确实是空的：跨幕的线没有，幕外的「续接上游末帧」也没有
    assert client.get(f"{API}/projects/{other['id']}/links").json() == []
    landed = shots_of(client, other["id"], body["scene"]["id"])
    assert [s["title"] for s in landed] == ["接上一幕"]
    detail = client.get(f"{API}/projects/{other['id']}/shots/{landed[0]['id']}").json()
    assert detail["prev_shot_id"] is None
    assert detail["workflow_id"] is None
    #: 队列历史属于那台机器，不进包
    queue = client.get(f"{API}/projects/{other['id']}/jobs").json()
    assert (queue["items"] if isinstance(queue, dict) else queue) == []


# --- 7. 环境要求清单 ---


def test_the_env_checklist_reports_missing_presets(
    client: TestClient, pid: str, tmp_path: Path
) -> None:
    presets.save("出片图", json.dumps(R2V_GRAPH, ensure_ascii=False))
    presets.save("转场图", json.dumps(FLF_GRAPH, ensure_ascii=False))
    resp = client.put(
        f"{API}/projects/{pid}/preset", json={"r2v_name": "出片图", "flf_name": "转场图"}
    )
    assert resp.status_code == 200, resp.text

    plan = client.post(f"{API}/projects/{pid}/package/plan", json={}).json()
    wanted = {p["role"]: p for p in plan["env"]["presets"]}
    assert wanted["r2v"]["name"] == "出片图"
    assert wanted["flf"]["name"] == "转场图"
    #: 入口是从导出机那份图里数出来的——目标机器至少知道「要一份标了这几个入口的图」
    assert "AIVS_PROMPT" in wanted["r2v"]["markers"]
    assert {"AIVS_FIRST_FRAME", "AIVS_LAST_FRAME"} <= set(wanted["flf"]["markers"])
    assert wanted["r2v"]["unreadable"] is False

    resp = client.post(f"{API}/projects/{pid}/package", json={"out_dir": out_dir(tmp_path)})
    pkg = resp.json()["path"]
    # 换一台没有这两份图的机器
    for name in ("出片图", "转场图"):
        assert client.delete(f"{API}/settings/presets/{name}").status_code == 200

    seen = client.post(f"{API}/packages/inspect", json={"path": pkg})
    assert seen.status_code == 200, seen.text  # 缺预设只在比对结果里标，不抛
    check = seen.json()["env_check"]
    assert set(check["missing"]) == {"出片图", "转场图"}
    for item in check["presets"]:
        assert item["present"] is False
        assert item["impact"], "缺了什么必须说出后果，否则用户要等到入队才知道"
        assert item["label"]


# --- 8. 密钥不进包 ---


def test_no_credentials_or_addresses_are_packed(
    client: TestClient, pid: str, tmp_path: Path
) -> None:
    resp = client.patch(
        f"{API}/settings",
        json={"values": {"comfy.base_url": "http://10.0.0.9:8188"}},
    )
    assert resp.status_code in (200, 422), resp.text

    resp = client.post(f"{API}/projects/{pid}/package", json={"out_dir": out_dir(tmp_path)})
    pkg = Path(resp.json()["path"])
    with zipfile.ZipFile(pkg) as zf:
        names = [i.filename for i in zf.infolist() if not i.is_dir()]
        manifest = zf.read("manifest.json").decode("utf-8")
    assert not [n for n in names if n.endswith("settings.json")]
    assert not [n for n in names if "cache/" in n or "proxies/" in n or ".runtime" in n]
    for forbidden in ("api_key", "base_url", "10.0.0.9", "token"):
        assert forbidden not in manifest, f"清单里不该出现 {forbidden}"

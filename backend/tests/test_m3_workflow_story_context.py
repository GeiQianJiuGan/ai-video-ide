"""Step 4-6 验收：Workflow 能力层、剧本/分镜、Context Resolver。

三个不变量在这里被钉住：
  1. 业务层只说 capability，换模型只换 workflow 行——所以「能力不可用」必须是
     一个能说出影响的结构化错误，而不是随便挑一条工作流；
  2. 手动路径必须能独立走完：LLM 没配置时只影响「AI 拆解」这一个按钮；
  3. 「到底喂了什么给模型」必须是一张可读账单，每条都能回答为什么被省略。
"""

from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient

from tests.conftest import BINDINGS, GRAPH, error_of, import_workflow, ready_workflow, upload_png

# --- Step 4：Workflow 能力层 ---


def test_import_rejects_broken_json(client: TestClient, pid: str) -> None:
    resp = client.post(
        f"/api/v1/projects/{pid}/workflows",
        json={"name": "坏图", "capability": "image2video", "api_json": "{不是 JSON"},
    )
    assert resp.status_code == 400
    err = error_of(resp)
    assert err["code"] == "INVALID_WORKFLOW"
    assert "JSON" in err["title"]
    assert any("API 格式" in s for s in err["suggestions"])


def test_import_rejects_non_api_shape(client: TestClient, pid: str) -> None:
    resp = client.post(
        f"/api/v1/projects/{pid}/workflows",
        json={"name": "界面图", "capability": "image2video", "api_json": "[]"},
    )
    assert resp.status_code == 400
    assert "API 格式" in error_of(resp)["title"]

    resp = client.post(
        f"/api/v1/projects/{pid}/workflows",
        json={
            "name": "缺 class_type",
            "capability": "image2video",
            "api_json": json.dumps({"1": {"inputs": {}}}),
        },
    )
    assert resp.status_code == 400
    err = error_of(resp)
    assert "节点结构" in err["title"]
    assert err["related_ids"]["nodes"] == ["1"]


def test_import_rejects_unknown_capability(client: TestClient, pid: str) -> None:
    resp = client.post(
        f"/api/v1/projects/{pid}/workflows",
        json={"name": "会飞", "capability": "text2music", "api_json": json.dumps(GRAPH)},
    )
    assert resp.status_code == 422
    assert "能力" in error_of(resp)["title"]


def test_imported_workflow_starts_as_draft_and_lists_nodes(client: TestClient, pid: str) -> None:
    row = import_workflow(client, pid, "image2video", bindings={"prompt": "6.text"})
    assert row["id"].startswith("wf_")
    assert row["status"] == "draft"
    assert row["missing_slots"] == ["reference_image"]
    assert row["required_nodes"] == ["LoadImage"]
    assert {n["id"] for n in row["nodes"]} == {"3", "6", "10", "11"}
    # 连线输入不该被列成可绑定字段
    assert next(n for n in row["nodes"] if n["id"] == "6")["fields"] == ["text"]


def test_validate_reports_missing_slot_and_marks_invalid(client: TestClient, pid: str) -> None:
    row = import_workflow(client, pid, "image2video", bindings={"prompt": "6.text"})
    resp = client.post(f"/api/v1/projects/{pid}/workflows/{row['id']}/validate?probe=false")
    assert resp.status_code == 400
    err = error_of(resp)
    assert err["code"] == "INVALID_WORKFLOW"
    assert "reference_image" in err["detail"]

    after = client.get(f"/api/v1/projects/{pid}/workflows/{row['id']}").json()
    assert after["status"] == "invalid"
    assert after["validation"]["ok"] is False
    assert after["validation"]["missing_slots"] == ["reference_image"]


def test_validate_catches_bad_binding_targets(client: TestClient, pid: str) -> None:
    row = import_workflow(
        client,
        pid,
        "image2video",
        bindings={"prompt": "6.clip", "reference_image": "99.image"},
    )
    resp = client.post(f"/api/v1/projects/{pid}/workflows/{row['id']}/validate?probe=false")
    assert resp.status_code == 400
    detail = error_of(resp)["detail"]
    assert "连线输入" in detail
    assert "没有节点 99" in detail


def test_bind_rejects_unknown_slot_and_resets_validation(client: TestClient, pid: str) -> None:
    row = ready_workflow(client, pid, "image2video")
    assert row["status"] == "ready"

    resp = client.put(
        f"/api/v1/projects/{pid}/workflows/{row['id']}/bindings",
        json={"bindings": {"prmopt": "6.text"}},
    )
    assert resp.status_code == 422
    assert "输入槽" in error_of(resp)["title"]

    rebound = client.put(
        f"/api/v1/projects/{pid}/workflows/{row['id']}/bindings",
        json={"bindings": BINDINGS["image2video"]},
    )
    assert rebound.status_code == 200, rebound.text
    # 改过绑定就必须重新校验，不能继续顶着 ready 跑
    assert rebound.json()["status"] == "draft"
    assert rebound.json()["validation"] is None


def test_capability_matrix_says_what_breaks(client: TestClient, pid: str) -> None:
    matrix = client.get(f"/api/v1/projects/{pid}/capabilities").json()
    rows = {r["capability"]: r for r in matrix["capabilities"]}
    assert set(rows) == {"text2image", "image2video", "first_last_frame", "upscale"}
    assert all(r["ready"] is False for r in rows.values())
    assert rows["image2video"]["impact"] == "缺少图生视频，绝大多数镜头无法生成。"
    assert rows["image2video"]["required_slots"] == ["prompt", "reference_image"]
    assert "online" in matrix["comfy"]

    wf = ready_workflow(client, pid, "image2video")
    rows = {
        r["capability"]: r
        for r in client.get(f"/api/v1/projects/{pid}/capabilities").json()["capabilities"]
    }
    assert rows["image2video"]["ready"] is True
    assert rows["image2video"]["impact"] is None
    assert rows["image2video"]["default_workflow_id"] == wf["id"]
    assert rows["text2image"]["ready"] is False


def test_default_and_delete_workflow(client: TestClient, pid: str) -> None:
    first = ready_workflow(client, pid, "image2video")
    second = import_workflow(client, pid, "image2video", name="备用流程")
    assert (
        client.post(f"/api/v1/projects/{pid}/workflows/{second['id']}/default").status_code == 200
    )

    listed = {
        r["id"]: r["is_default"] for r in client.get(f"/api/v1/projects/{pid}/workflows").json()
    }
    assert listed == {first["id"]: 0, second["id"]: 1}

    assert client.delete(f"/api/v1/projects/{pid}/workflows/{second['id']}").status_code == 204
    assert client.get(f"/api/v1/projects/{pid}/workflows/{second['id']}").status_code == 404
    assert error_of(client.get(f"/api/v1/projects/{pid}/workflows/wf_nope"))["code"] == "NOT_FOUND"


# --- Step 5：剧本 / Scene / Shot / 分镜板 ---


def make_scene(client: TestClient, pid: str, title: str = "第一场", **patch: Any) -> dict[str, Any]:
    resp = client.post(f"/api/v1/projects/{pid}/scenes", json={"title": title, **patch})
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


def make_shot(client: TestClient, pid: str, sid: str, **patch: Any) -> dict[str, Any]:
    resp = client.post(f"/api/v1/projects/{pid}/scenes/{sid}/shots", json=patch)
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


def test_story_reports_llm_status_and_keeps_manual_path_open(client: TestClient, pid: str) -> None:
    story = client.get(f"/api/v1/projects/{pid}/story").json()
    assert story["id"].startswith("sty_")
    assert story["mode"] == "manual"
    assert story["llm"]["configured"] is False
    assert "手动模式" in story["llm"]["hint"]

    saved = client.patch(
        f"/api/v1/projects/{pid}/story", json={"title": "雨夜", "raw_text": "林昭走进旧宅。"}
    ).json()
    assert saved["raw_text"] == "林昭走进旧宅。"
    assert client.get(f"/api/v1/projects/{pid}/story").json()["title"] == "雨夜"


def test_scene_defaults_and_ordering(client: TestClient, pid: str) -> None:
    blank = client.post(f"/api/v1/projects/{pid}/scenes", json={}).json()
    assert blank["title"] == "新场景"
    assert blank["index_no"] == 1
    second = make_scene(client, pid, "第二场")
    assert second["index_no"] == 2

    reordered = client.put(
        f"/api/v1/projects/{pid}/scenes/order", json={"order": [second["id"], blank["id"]]}
    ).json()
    assert [s["id"] for s in reordered] == [second["id"], blank["id"]]
    assert [s["index_no"] for s in reordered] == [1, 2]

    resp = client.put(f"/api/v1/projects/{pid}/scenes/order", json={"order": ["scn_nope"]})
    assert resp.status_code == 422
    assert "不存在的场景" in error_of(resp)["title"]

    assert client.delete(f"/api/v1/projects/{pid}/scenes/{second['id']}").status_code == 204
    assert [s["index_no"] for s in client.get(f"/api/v1/projects/{pid}/scenes").json()] == [1]


def test_scene_rejects_unknown_variant(client: TestClient, pid: str) -> None:
    resp = client.post(
        f"/api/v1/projects/{pid}/scenes", json={"title": "夜", "location_variant_id": "var_nope"}
    )
    assert resp.status_code == 404
    assert "地点变体" in error_of(resp)["title"]


def test_shot_defaults_and_status_validation(client: TestClient, pid: str) -> None:
    scene = make_scene(client, pid)
    shot = make_shot(client, pid, scene["id"])
    assert shot["duration"] == 4.0
    assert shot["status"] == "draft"
    assert shot["index_no"] == 1

    resp = client.post(
        f"/api/v1/projects/{pid}/scenes/{scene['id']}/shots", json={"status": "半成品"}
    )
    assert resp.status_code == 422
    assert "镜头状态" in error_of(resp)["title"]

    resp = client.patch(f"/api/v1/projects/{pid}/shots/{shot['id']}", json={"status": "半成品"})
    assert resp.status_code == 422

    detail = client.get(f"/api/v1/projects/{pid}/shots/{shot['id']}").json()
    assert detail["scene_title"] == "第一场"
    assert detail["version_count"] == 0
    assert detail["cast"] == [] and detail["props"] == []


def test_shot_dependency_guards(client: TestClient, pid: str) -> None:
    scene = make_scene(client, pid)
    a = make_shot(client, pid, scene["id"], title="A")
    b = make_shot(client, pid, scene["id"], title="B")

    resp = client.patch(f"/api/v1/projects/{pid}/shots/{a['id']}", json={"prev_shot_id": a["id"]})
    assert resp.status_code == 400
    err = error_of(resp)
    assert err["code"] == "DEPENDENCY_CYCLE"
    assert "不能依赖自己" in err["title"]

    assert (
        client.patch(
            f"/api/v1/projects/{pid}/shots/{b['id']}", json={"prev_shot_id": a["id"]}
        ).status_code
        == 200
    )
    resp = client.patch(f"/api/v1/projects/{pid}/shots/{a['id']}", json={"prev_shot_id": b["id"]})
    assert resp.status_code == 400
    assert "成环" in error_of(resp)["title"]

    # A 是 B 的上游，删 A 必须先说清后果
    resp = client.delete(f"/api/v1/projects/{pid}/shots/{a['id']}")
    assert resp.status_code == 409
    err = error_of(resp)
    assert err["code"] == "CONFLICT"
    assert err["related_ids"]["dependents"] == [b["id"]]

    resp = client.patch(
        f"/api/v1/projects/{pid}/shots/{b['id']}", json={"prev_shot_id": "sht_nope"}
    )
    assert resp.status_code == 404
    assert "上游镜头" in error_of(resp)["title"]


def test_move_shot_resequences_globally(client: TestClient, pid: str) -> None:
    one = make_scene(client, pid, "第一场")
    two = make_scene(client, pid, "第二场")
    a = make_shot(client, pid, one["id"], title="A")
    b = make_shot(client, pid, one["id"], title="B")
    c = make_shot(client, pid, two["id"], title="C")
    assert [s["index_no"] for s in (a, b, c)] == [1, 2, 3]

    lanes = client.post(
        f"/api/v1/projects/{pid}/shots/{b['id']}/move",
        json={"scene_id": two["id"], "position": 0},
    ).json()
    flat = [(card["title"], card["index_no"]) for lane in lanes for card in lane["shots"]]
    assert flat == [("A", 1), ("B", 2), ("C", 3)]
    assert [lane["title"] for lane in lanes] == ["第一场", "第二场"]
    assert [c["title"] for c in lanes[1]["shots"]] == ["B", "C"]

    resp = client.put(
        f"/api/v1/projects/{pid}/scenes/{one['id']}/shots/order", json={"order": [c["id"]]}
    )
    assert resp.status_code == 422
    assert "不属于该场景" in error_of(resp)["title"]


def test_storyboard_cards_carry_context_issues(client: TestClient, pid: str) -> None:
    scene = make_scene(client, pid)
    make_shot(client, pid, scene["id"], title="空镜")
    card = client.get(f"/api/v1/projects/{pid}/storyboard").json()[0]["shots"][0]
    assert card["context_ok"] is False
    assert card["context_issues"] == [
        "缺少地点变体，Context 不完整",
        "没有出场角色",
        "没有 prompt 也没有画面描述",
    ]
    assert card["thumbnail_asset_id"] is None
    assert card["version_count"] == 0


def test_shot_cast_and_props_round_trip(client: TestClient, pid: str) -> None:
    char = client.post(f"/api/v1/projects/{pid}/characters", json={"name": "林昭"}).json()
    app_id = client.get(f"/api/v1/projects/{pid}/characters/{char['id']}/appearances").json()[0][
        "id"
    ]
    prop = client.post(f"/api/v1/projects/{pid}/props", json={"name": "油纸伞"}).json()
    scene = make_scene(client, pid)
    shot = make_shot(client, pid, scene["id"])

    detail = client.put(
        f"/api/v1/projects/{pid}/shots/{shot['id']}/cast", json={"appearance_ids": [app_id]}
    ).json()
    assert detail["cast"][0]["character_name"] == "林昭"
    assert detail["cast"][0]["appearance_name"] == "默认形象"

    detail = client.put(
        f"/api/v1/projects/{pid}/shots/{shot['id']}/props",
        json={"items": [{"prop_id": prop["id"], "state": "discarded"}]},
    ).json()
    assert detail["props"][0]["prop_name"] == "油纸伞"
    assert detail["props"][0]["state"] == "discarded"

    resp = client.put(
        f"/api/v1/projects/{pid}/shots/{shot['id']}/cast", json={"appearance_ids": ["app_nope"]}
    )
    assert resp.status_code == 404
    assert "形象" in error_of(resp)["title"]
    # 失败的整表替换不该留下半套数据
    assert len(client.get(f"/api/v1/projects/{pid}/shots/{shot['id']}").json()["cast"]) == 1


def test_breakdown_without_llm_points_at_the_manual_path(client: TestClient, pid: str) -> None:
    resp = client.post(f"/api/v1/projects/{pid}/breakdown/propose", json={"text": "  "})
    assert resp.status_code == 422
    err = error_of(resp)
    assert "剧本是空的" in err["title"]
    assert any("手动" in s for s in err["suggestions"])

    resp = client.post(f"/api/v1/projects/{pid}/breakdown/propose", json={"text": "林昭走进旧宅。"})
    assert resp.status_code == 503
    err = error_of(resp)
    assert err["code"] == "LLM_UNAVAILABLE"
    assert any("手动" in s for s in err["suggestions"])


def test_breakdown_apply_is_a_pure_manual_path(client: TestClient, pid: str) -> None:
    """提案对象可以完全由人手写：apply 不需要 LLM 参与。"""
    resp = client.post(
        f"/api/v1/projects/{pid}/breakdown/apply",
        json={
            "scenes": [
                {
                    "op": "add",
                    "title": "雨夜旧宅",
                    "time_of_day": "夜",
                    "shots": [
                        {"op": "add", "title": "巷口", "duration": 3},
                        {"op": "reject", "title": "被否掉的镜头"},
                    ],
                },
                {"op": "reject", "title": "整场否掉", "shots": [{"op": "add", "title": "x"}]},
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"scenes_created": 1, "shots_created": 1}
    assert [s["title"] for s in client.get(f"/api/v1/projects/{pid}/scenes").json()] == ["雨夜旧宅"]
    assert client.get(f"/api/v1/projects/{pid}/story").json()["mode"] == "ai_assisted"


# --- Step 6：Context Resolver ---


def full_shot(client: TestClient, pid: str) -> dict[str, Any]:
    """凑出一个上下文完整的镜头：地点变体 + 有角色表的形象 + prompt。"""
    loc = client.post(f"/api/v1/projects/{pid}/locations", json={"name": "城南旧宅"}).json()
    variant = client.post(
        f"/api/v1/projects/{pid}/locations/{loc['id']}/variants",
        json={"name": "雨夜", "time_of_day": "夜"},
    ).json()
    client.post(
        f"/api/v1/projects/{pid}/variants/{variant['id']}/references",
        json={
            "asset_id": upload_png(client, pid, "location_reference", "loc.png"),
            "camera": "35mm",
        },
    )
    char = client.post(f"/api/v1/projects/{pid}/characters", json={"name": "林昭"}).json()
    app_id = client.get(f"/api/v1/projects/{pid}/characters/{char['id']}/appearances").json()[0][
        "id"
    ]
    client.post(
        f"/api/v1/projects/{pid}/appearances/{app_id}/sheets",
        json={"asset_id": upload_png(client, pid, "character_sheet", "sheet.png")},
    )
    scene = make_scene(client, pid, "第一场", location_variant_id=variant["id"])
    shot = make_shot(client, pid, scene["id"], title="推近", prompt="雨夜，林昭推门")
    client.put(f"/api/v1/projects/{pid}/shots/{shot['id']}/cast", json={"appearance_ids": [app_id]})
    return {"scene": scene, "shot": shot, "variant": variant, "appearance_id": app_id}


def test_context_lists_every_problem_when_empty(client: TestClient, pid: str) -> None:
    scene = make_scene(client, pid)
    shot = make_shot(client, pid, scene["id"])
    ctx = client.get(f"/api/v1/projects/{pid}/shots/{shot['id']}/context").json()
    assert ctx["complete"] is False
    assert ctx["problems"] == [
        "本 Scene 还没有选定地点变体",
        "本镜头没有出场角色",
        "既没有 prompt 也没有画面描述",
    ]
    assert ctx["items"] == []
    assert ctx["included_count"] == 0
    # 「能收几张参考图」不再是应用级设置，而是问适配层；没选预设时就是不限张数。
    assert ctx["capacity"]["limit"] is None
    assert ctx["capacity"]["over"] is False
    assert ctx["capacity"]["ref_count"] == 0


def test_context_bill_explains_priority_and_source(client: TestClient, pid: str) -> None:
    made = full_shot(client, pid)
    ctx = client.get(f"/api/v1/projects/{pid}/shots/{made['shot']['id']}/context").json()
    assert ctx["complete"] is True
    assert ctx["problems"] == []
    kinds = [(i["kind"], i["included"], i["priority"]) for i in ctx["items"]]
    assert kinds == [("character_sheet", True, 100), ("location_reference", True, 90)]
    # 首帧只认显式指定：这个镜头没填槽位、也没有上游镜头，所以两条都是参考素材
    # ——以前优先级最高的那张会被提拔成首帧，于是角色三视图成了画面第一格。
    assert [i["role"] for i in ctx["items"]] == ["reference", "reference"]
    assert [i["media"] for i in ctx["items"]] == ["image", "image"]
    sheet = ctx["items"][0]
    assert sheet["reason"] == "该角色在本镜头出场"
    assert sheet["asset_path"].startswith("assets/character_sheets/")
    assert sheet["missing_file"] is False
    assert ctx["included_count"] == 2


def test_context_drops_a_second_appearance_of_the_same_character(
    client: TestClient, pid: str
) -> None:
    made = full_shot(client, pid)
    char = client.get(f"/api/v1/projects/{pid}/characters").json()[0]
    other = client.post(
        f"/api/v1/projects/{pid}/characters/{char['id']}/appearances", json={"name": "少年版"}
    ).json()
    client.post(
        f"/api/v1/projects/{pid}/appearances/{other['id']}/sheets",
        json={"asset_id": upload_png(client, pid, "character_sheet", "sheet2.png")},
    )
    client.put(
        f"/api/v1/projects/{pid}/shots/{made['shot']['id']}/cast",
        json={"appearance_ids": [made["appearance_id"], other["id"]]},
    )
    ctx = client.get(f"/api/v1/projects/{pid}/shots/{made['shot']['id']}/context").json()
    dropped = [i for i in ctx["items"] if i["kind"] == "character_sheet" and not i["included"]]
    assert len(dropped) == 1
    assert dropped[0]["reason"] == "同一角色已有更高优先级形象"
    assert ctx["included_count"] == 2


def test_context_overrides_remove_add_and_reset(client: TestClient, pid: str) -> None:
    made = full_shot(client, pid)
    shot_id = made["shot"]["id"]
    base = client.get(f"/api/v1/projects/{pid}/shots/{shot_id}/context").json()
    sheet_key = next(i["key"] for i in base["items"] if i["kind"] == "character_sheet")

    ctx = client.post(
        f"/api/v1/projects/{pid}/shots/{shot_id}/context/override",
        json={"action": "remove", "key": sheet_key},
    ).json()
    removed = next(i for i in ctx["items"] if i["key"] == sheet_key)
    assert removed["included"] is False
    assert removed["reason"] == "手动移除"
    assert ctx["included_count"] == 1

    manual_asset = upload_png(client, pid, "upload", "manual.png")
    ctx = client.post(
        f"/api/v1/projects/{pid}/shots/{shot_id}/context/override",
        json={"action": "add", "asset_id": manual_asset, "label": "导演指定的光比参考"},
    ).json()
    manual = next(i for i in ctx["items"] if i["kind"] == "manual")
    assert manual["included"] is True
    assert manual["priority"] == 110  # 人工条目永不被自动逻辑挤掉
    assert manual["label"] == "导演指定的光比参考"

    ctx = client.post(
        f"/api/v1/projects/{pid}/shots/{shot_id}/context/override", json={"action": "reset"}
    ).json()
    assert ctx["overrides"] == []
    assert ctx["included_count"] == 2


def test_context_override_rejects_bad_input(client: TestClient, pid: str) -> None:
    made = full_shot(client, pid)
    shot_id = made["shot"]["id"]
    resp = client.post(
        f"/api/v1/projects/{pid}/shots/{shot_id}/context/override", json={"action": "replace"}
    )
    assert resp.status_code == 422
    assert "干预动作" in error_of(resp)["title"]

    resp = client.post(
        f"/api/v1/projects/{pid}/shots/{shot_id}/context/override", json={"action": "remove"}
    )
    assert resp.status_code == 422
    assert "移除" in error_of(resp)["title"]

    resp = client.post(
        f"/api/v1/projects/{pid}/shots/{shot_id}/context/override", json={"action": "add"}
    )
    assert resp.status_code == 422
    assert "资产" in error_of(resp)["title"]

    resp = client.post(
        f"/api/v1/projects/{pid}/shots/{shot_id}/context/override",
        json={"action": "add", "asset_id": "ast_nope"},
    )
    assert resp.status_code == 404


def test_context_snapshot_records_included_and_omitted(client: TestClient, pid: str) -> None:
    made = full_shot(client, pid)
    shot_id = made["shot"]["id"]
    base = client.get(f"/api/v1/projects/{pid}/shots/{shot_id}/context").json()
    sheet_key = next(i["key"] for i in base["items"] if i["kind"] == "character_sheet")
    client.post(
        f"/api/v1/projects/{pid}/shots/{shot_id}/context/override",
        json={"action": "remove", "key": sheet_key},
    )
    snap = client.get(f"/api/v1/projects/{pid}/shots/{shot_id}/context/snapshot").json()
    assert [i["kind"] for i in snap["included"]] == ["location_reference"]
    # 角色 / 地点这些参考素材永远不会被提拔成首帧，剩一条也一样
    assert [i["role"] for i in snap["included"]] == ["reference"]
    assert [i["media"] for i in snap["included"]] == ["image"]
    assert [(i["key"], i["reason"]) for i in snap["omitted"]] == [(sheet_key, "手动移除")]
    # 当时模型端能收几张也一起冻结，事后才说得清「为什么少喂了两张」
    assert snap["capacity"]["limit"] is None
    assert [i["over_capacity"] for i in snap["included"]] == [False]
    assert snap["resolved_at"]


def test_context_of_unknown_shot_is_a_structured_404(client: TestClient, pid: str) -> None:
    resp = client.get(f"/api/v1/projects/{pid}/shots/sht_nope/context")
    assert resp.status_code == 404
    assert "镜头" in error_of(resp)["title"]

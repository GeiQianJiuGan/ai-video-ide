"""Step 3 验收：场景衔接与两种编排。

这个文件盯的是「编排到底做了什么」，而不是「有没有报错」：

  1. `plan` 是账单——要生成几条、要补几段转场、缺什么，按下去之前就得看见；
  2. `parallel` 补出来的转场镜头必须**属于上一幕且排在最后**，否则时间线自动装配
     会把它放错位置（导出侧一行没改，靠的就是这个顺序）；
  3. `sequential` 必须串出 `prev_shot_id` 链，并且下游任务是**可解释的等待**
     （`wait_reason` 写明在等谁），不是卡住；
  4. 单线程模式下图上的 `transition` 会被忽略——这件事必须写在账单里，不能默默换掉。

所有入队的用例先 `POST /queue/pause`，pump 就不会真去连 ComfyUI。
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from tests.conftest import error_of, upload_png

API = "/api/v1"


def make_world(client: TestClient, pid: str) -> tuple[str, str]:
    """凑一套「上下文能过门槛」的世界：一个有参考图的地点变体 + 一个有角色表的形象。

    照 `test_m4_*::complete_shot` 的写法——编排的账单要真能兑现，就不能拿一个
    上下文不完整的镜头去测，那样测到的只是门槛本身。
    """
    loc = client.post(f"{API}/projects/{pid}/locations", json={"name": "城南旧宅"}).json()
    variant = client.post(
        f"{API}/projects/{pid}/locations/{loc['id']}/variants",
        json={"name": "雨夜", "time_of_day": "夜"},
    ).json()
    client.post(
        f"{API}/projects/{pid}/variants/{variant['id']}/references",
        json={"asset_id": upload_png(client, pid, "location_reference", "loc.png")},
    )
    char = client.post(f"{API}/projects/{pid}/characters", json={"name": "林昭"}).json()
    app_id = client.get(f"{API}/projects/{pid}/characters/{char['id']}/appearances").json()[0]["id"]
    client.post(
        f"{API}/projects/{pid}/appearances/{app_id}/sheets",
        json={"asset_id": upload_png(client, pid, "character_sheet", "sheet.png")},
    )
    return str(variant["id"]), str(app_id)


def make_scene(client: TestClient, pid: str, title: str, **patch: Any) -> str:
    resp = client.post(f"{API}/projects/{pid}/scenes", json={"title": title, **patch})
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def make_shot(client: TestClient, pid: str, sid: str, title: str, **patch: Any) -> str:
    resp = client.post(
        f"{API}/projects/{pid}/scenes/{sid}/shots",
        json={"title": title, "prompt": f"{title} 的画面", **patch},
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def cast(client: TestClient, pid: str, shot_id: str, app_id: str) -> None:
    resp = client.put(
        f"{API}/projects/{pid}/shots/{shot_id}/cast", json={"appearance_ids": [app_id]}
    )
    assert resp.status_code == 200, resp.text


def two_scenes(client: TestClient, pid: str) -> tuple[str, str, str, str]:
    """两幕、各一个上下文完整的镜头。返回 (幕1, 幕2, 镜头1, 镜头2)。"""
    variant, app_id = make_world(client, pid)
    a = make_scene(client, pid, "雨夜追车", location_variant_id=variant)
    b = make_scene(client, pid, "天台对峙", location_variant_id=variant)
    sa = make_shot(client, pid, a, "车灯划过水面")
    sb = make_shot(client, pid, b, "推门上天台")
    cast(client, pid, sa, app_id)
    cast(client, pid, sb, app_id)
    return a, b, sa, sb


def test_link_modes_round_trip(client: TestClient, pid: str) -> None:
    a, b, _, _ = two_scenes(client, pid)
    resp = client.put(
        f"{API}/projects/{pid}/links",
        json={"from_scene_id": a, "to_scene_id": b, "mode": "transition", "duration": 1.5},
    )
    assert resp.status_code == 200, resp.text
    link = resp.json()
    assert link["mode"] == "transition"
    assert link["hint"], "每种衔接方式都要带一句人能看懂的解释"
    assert link["from_index_no"] == 1 and link["to_index_no"] == 2

    # 同一对场景只有一条：再 PUT 一次是改，不是多出一条
    again = client.put(
        f"{API}/projects/{pid}/links",
        json={"from_scene_id": a, "to_scene_id": b, "mode": "tail_frame"},
    )
    assert again.status_code == 200, again.text
    assert again.json()["id"] == link["id"]
    assert again.json()["mode"] == "tail_frame"
    assert len(client.get(f"{API}/projects/{pid}/links").json()) == 1

    graph = client.get(f"{API}/projects/{pid}/flow").json()
    assert [n["index_no"] for n in graph["nodes"]] == [1, 2]
    assert len(graph["links"]) == 1
    assert {m["name"] for m in graph["modes"]} == {"cut", "transition", "tail_frame"}


def test_bad_link_says_what_is_allowed(client: TestClient, pid: str) -> None:
    a, b, _, _ = two_scenes(client, pid)
    resp = client.put(
        f"{API}/projects/{pid}/links",
        json={"from_scene_id": a, "to_scene_id": b, "mode": "morph"},
    )
    assert resp.status_code == 422
    err = error_of(resp)
    assert err["code"] == "VALIDATION_ERROR"
    assert any("转场" in s for s in err["suggestions"]), "报错要把三种衔接方式说清"

    same = client.put(
        f"{API}/projects/{pid}/links",
        json={"from_scene_id": a, "to_scene_id": a, "mode": "cut"},
    )
    assert same.status_code == 422
    error_of(same)

    long_one = client.put(
        f"{API}/projects/{pid}/links",
        json={"from_scene_id": a, "to_scene_id": b, "mode": "transition", "duration": 20},
    )
    assert long_one.status_code == 422
    assert "20" in error_of(long_one)["detail"], "报错要说出用户给的那个值"


def test_plan_is_a_bill_and_names_what_is_missing(client: TestClient, pid: str) -> None:
    variant, app_id = make_world(client, pid)
    a = make_scene(client, pid, "雨夜追车", location_variant_id=variant)
    b = make_scene(client, pid, "天台对峙", location_variant_id=variant)
    shot = make_shot(client, pid, a, "车灯划过水面")
    cast(client, pid, shot, app_id)
    make_scene(client, pid, "空的一幕")  # 没有镜头，必须出现在 blockers 里
    client.put(
        f"{API}/projects/{pid}/links",
        json={"from_scene_id": a, "to_scene_id": b, "mode": "transition"},
    )

    bill = client.post(f"{API}/projects/{pid}/sequence/plan", json={"mode": "parallel"}).json()
    assert bill["mode"] == "parallel"
    assert [row["index_no"] for row in bill["scenes"]] == [1, 2, 3]
    assert bill["total_jobs"] == 1, "只有一个镜头能生成，转场做不出来（第 2 幕没镜头）"
    assert bill["transitions_to_create"] == 0
    assert bill["scenes"][0]["ready_count"] == 1
    empties = [row["why"] for row in bill["blockers"]]
    assert any("还没有镜头" in why for why in empties)
    assert any("转场做不出来" in why for why in empties)
    assert any("账单" in note for note in bill["notes"]), "账单必须自己说明「还没有入队」"

    # plan 是只读的：没有任何任务被入队
    assert client.get(f"{API}/projects/{pid}/jobs").json() == []


def test_plan_says_which_shot_will_be_skipped(client: TestClient, pid: str) -> None:
    """上下文不完整的镜头不能算进 total_jobs——账单不能承诺兑现不了的事。"""
    scene = make_scene(client, pid, "雨夜追车")  # 没有地点变体
    make_shot(client, pid, scene, "车灯划过水面")  # 也没有出场角色
    bill = client.post(f"{API}/projects/{pid}/sequence/plan", json={"mode": "parallel"}).json()
    assert bill["total_jobs"] == 0
    assert bill["scenes"][0]["shot_count"] == 1 and bill["scenes"][0]["ready_count"] == 0
    assert bill["scenes"][0]["missing"], "缺地点变体与出场角色，账单里要逐条写出来"
    row = next(r for r in bill["blockers"] if r.get("shot_id"))
    assert "会被跳过" in row["why"] and "地点变体" in row["why"]
    assert row["how"]


def test_plan_needs_at_least_one_scene(client: TestClient, pid: str) -> None:
    resp = client.post(f"{API}/projects/{pid}/sequence/plan", json={"mode": "parallel"})
    assert resp.status_code == 422
    assert any("加一幕" in s for s in error_of(resp)["suggestions"])


def test_unknown_mode_lists_the_two_modes(client: TestClient, pid: str) -> None:
    two_scenes(client, pid)
    resp = client.post(f"{API}/projects/{pid}/sequence/plan", json={"mode": "magic"})
    assert resp.status_code == 422
    err = error_of(resp)
    assert any("parallel" in s for s in err["suggestions"])
    assert any("sequential" in s for s in err["suggestions"])


def test_parallel_puts_the_transition_at_the_end_of_the_first_scene(
    client: TestClient, pid: str
) -> None:
    assert client.post(f"{API}/projects/{pid}/queue/pause").status_code == 200
    a, b, sa, _ = two_scenes(client, pid)
    client.put(
        f"{API}/projects/{pid}/links",
        json={"from_scene_id": a, "to_scene_id": b, "mode": "transition", "duration": 1.2},
    )
    bill = client.post(f"{API}/projects/{pid}/sequence/plan", json={"mode": "parallel"}).json()
    assert bill["transitions_to_create"] == 1
    assert bill["total_jobs"] == 3, "两个正片镜头 + 一段转场"

    resp = client.post(f"{API}/projects/{pid}/sequence/run", json={"mode": "parallel"})
    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert out["skipped"] == []
    assert len(out["queued"]) == 2
    assert len(out["transitions"]) == 1 and out["transitions"][0]["reused"] is False

    lanes = client.get(f"{API}/projects/{pid}/storyboard").json()
    first = next(lane for lane in lanes if lane["id"] == a)
    # 转场属于上一幕、排在最后——时间线自动装配靠这个顺序把它放到两幕之间
    assert [card["id"] for card in first["shots"]][0] == sa
    made = out["transitions"][0]["shot_id"]
    assert [card["id"] for card in first["shots"]][-1] == made
    shot = client.get(f"{API}/projects/{pid}/shots/{made}").json()
    assert shot["kind"] == "transition"
    assert shot["scene_id"] == a
    assert shot["prev_shot_id"] == sa, "转场的首帧来自上一幕的真末帧"
    assert shot["duration"] == 1.2
    tail = next(lane for lane in lanes if lane["id"] == b)
    assert all(card["id"] != made for card in tail["shots"]), "转场不属于下一幕"

    # 转场任务在等上游出片，而且等待原因是可解释的
    jobs = client.get(f"{API}/projects/{pid}/jobs").json()
    waiting = [j for j in jobs if j["shot_id"] == made]
    assert len(waiting) == 1
    assert waiting[0]["status"] == "waiting"
    assert "末帧" in (waiting[0]["wait_reason"] or "")


def test_transition_needs_a_first_frame_on_the_next_scene(client: TestClient, pid: str) -> None:
    assert client.post(f"{API}/projects/{pid}/queue/pause").status_code == 200
    variant, app_id = make_world(client, pid)
    a = make_scene(client, pid, "雨夜追车", location_variant_id=variant)
    b = make_scene(client, pid, "天台对峙")  # 没有地点变体
    sa = make_shot(client, pid, a, "车灯划过水面")
    make_shot(client, pid, b, "推门上天台")  # 也没有出场角色，于是一张首帧图都取不到
    cast(client, pid, sa, app_id)
    client.put(
        f"{API}/projects/{pid}/links",
        json={"from_scene_id": a, "to_scene_id": b, "mode": "transition"},
    )
    bill = client.post(f"{API}/projects/{pid}/sequence/plan", json={"mode": "parallel"}).json()
    assert bill["transitions_to_create"] == 0
    edge = bill["links"][0]
    assert edge["will_create_transition"] is False
    assert "首帧图" in edge["blocked"]
    assert any("硬切" in row["how"] for row in bill["blockers"]), "做不出来时要指出另一条路"


def test_sequential_chains_the_shots_and_explains_the_wait(client: TestClient, pid: str) -> None:
    assert client.post(f"{API}/projects/{pid}/queue/pause").status_code == 200
    a, b, sa, sb = two_scenes(client, pid)
    # 上游即使已经有旧版本，本次续接也必须等本次新任务完成，不能拿旧指针提前放行。
    old_asset = upload_png(client, pid, "generated_video", "old-head.png")
    assert client.post(
        f"{API}/projects/{pid}/shots/{sa}/versions",
        json={"asset_id": old_asset, "kind": "video"},
    ).status_code == 201
    client.put(
        f"{API}/projects/{pid}/links",
        json={"from_scene_id": a, "to_scene_id": b, "mode": "transition"},
    )
    bill = client.post(f"{API}/projects/{pid}/sequence/plan", json={"mode": "sequential"}).json()
    assert bill["transitions_to_create"] == 0
    assert bill["ignored_transitions"] == 1
    assert bill["links"][0]["effective"] == "tail_frame"
    assert any("被当成" in note for note in bill["notes"]), "换掉用户配的东西必须说出来"

    resp = client.post(f"{API}/projects/{pid}/sequence/run", json={"mode": "sequential"})
    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert out["chain"] == [sa, sb]
    assert len(out["queued"]) == 2
    assert out["transitions"] == [], "单线程续接不生成转场"

    downstream = client.get(f"{API}/projects/{pid}/shots/{sb}").json()
    assert downstream["prev_shot_id"] == sa
    head = client.get(f"{API}/projects/{pid}/shots/{sa}").json()
    assert head["prev_shot_id"] is None, "链头不依赖任何镜头"

    jobs = client.get(f"{API}/projects/{pid}/jobs").json()
    tail_job = next(j for j in jobs if j["shot_id"] == sb)
    assert tail_job["status"] == "waiting"
    assert "本次生成" in (tail_job["wait_reason"] or "")
    head_job = next(j for j in jobs if j["shot_id"] == sa)
    assert head_job["status"] == "queued", "链头不该等任何人"
    assert tail_job["params"]["wait_for_job_id"] == head_job["id"]


def test_transition_with_a_version_is_not_regenerated(client: TestClient, pid: str) -> None:
    assert client.post(f"{API}/projects/{pid}/queue/pause").status_code == 200
    a, b, _, _ = two_scenes(client, pid)
    client.put(
        f"{API}/projects/{pid}/links",
        json={"from_scene_id": a, "to_scene_id": b, "mode": "transition"},
    )
    first = client.post(f"{API}/projects/{pid}/sequence/run", json={"mode": "parallel"}).json()
    made = first["transitions"][0]["shot_id"]
    # 手工给转场造一个成片版本，模拟「这段已经出片了」
    asset_id = upload_png(client, pid, "generated_video", "trans.png")
    assert (
        client.post(
            f"{API}/projects/{pid}/shots/{made}/versions",
            json={"asset_id": asset_id, "kind": "video"},
        ).status_code
        == 201
    )
    again = client.post(f"{API}/projects/{pid}/sequence/run", json={"mode": "parallel"}).json()
    reused = again["transitions"][0]
    assert reused["shot_id"] == made
    assert reused["reused"] is True, "版本永不覆盖：已出片的转场不重做"
    assert reused["job_id"] is None

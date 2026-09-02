"""幕的小节点（prompt / 人物 / 地点）与节点上播的那一段。

这个文件盯的是「流程图上那个节点到底说了什么」，四件事：

  1. **prompt 是唯一必填的小节点**——幕级 prompt 要真的兜底镜头：上下文账单、分镜板、
     入队参数三处口径必须一致，否则它只是个装饰输入框；
  2. **人物 / 地点可以多选也可以不选**，但各自不能超过上限，超了要报四要素错误，
     并且**说清上限在哪儿改**；
  3. **上限真的可配置**——改 `scene.node_limit` 立刻生效，不用重启；
  4. **「用哪一段」是镜头级的，幕上没有第二个指针**：一幕下面很多镜头，每个镜头各自
     生成很多段，采用走全工程唯一那个入口 `POST /versions/{id}/current`
     （= `Shot.current_version_id`），时间线装配认的就是它。节点上播哪一段只是「挑一段
     来看」，挑到的不是采用的那一版时要标出来（`video_adopted=false`），不假装是。

入队的用例先 `POST /queue/pause`，pump 就不会真去连 ComfyUI。
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from tests.conftest import error_of, upload_png

API = "/api/v1"


def upload_mp4(client: TestClient, pid: str, name: str = "clip.mp4") -> str:
    """上传一段假视频。内容不重要，**后缀重要**——节点要靠它区分「能播的」与「图」。"""
    resp = client.post(
        f"{API}/projects/{pid}/assets/upload",
        data={"kind": "generated_video"},
        files={"file": (name, b"FAKEMP4" + name.encode(), "video/mp4")},
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def make_variant(client: TestClient, pid: str, loc_name: str, variant_name: str) -> str:
    loc = client.post(f"{API}/projects/{pid}/locations", json={"name": loc_name}).json()
    variant = client.post(
        f"{API}/projects/{pid}/locations/{loc['id']}/variants", json={"name": variant_name}
    ).json()
    client.post(
        f"{API}/projects/{pid}/variants/{variant['id']}/references",
        json={"asset_id": upload_png(client, pid, "location_reference", f"{variant_name}.png")},
    )
    return str(variant["id"])


def make_appearance(client: TestClient, pid: str, name: str, *, sheet: bool = True) -> str:
    char = client.post(f"{API}/projects/{pid}/characters", json={"name": name}).json()
    app_id = client.get(f"{API}/projects/{pid}/characters/{char['id']}/appearances").json()[0]["id"]
    if sheet:
        client.post(
            f"{API}/projects/{pid}/appearances/{app_id}/sheets",
            json={"asset_id": upload_png(client, pid, "character_sheet", f"{name}.png")},
        )
    return str(app_id)


def make_scene(client: TestClient, pid: str, title: str, **patch: Any) -> str:
    resp = client.post(f"{API}/projects/{pid}/scenes", json={"title": title, **patch})
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def make_shot(client: TestClient, pid: str, sid: str, title: str, **patch: Any) -> str:
    resp = client.post(f"{API}/projects/{pid}/scenes/{sid}/shots", json={"title": title, **patch})
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def node_of(client: TestClient, pid: str, sid: str) -> dict[str, Any]:
    graph = client.get(f"{API}/projects/{pid}/flow").json()
    node = next((n for n in graph["nodes"] if n["id"] == sid), None)
    assert node is not None, "流程图里找不到这一幕"
    return dict(node)


# --- 1. 小节点的增删（人物 / 地点可多选，也可以一个都不选） ---


def test_scene_nodes_round_trip(client: TestClient, pid: str) -> None:
    v1 = make_variant(client, pid, "城南旧宅", "雨夜")
    v2 = make_variant(client, pid, "天台", "黄昏")
    app_id = make_appearance(client, pid, "林昭")
    sid = make_scene(client, pid, "雨夜追车", prompt="雨夜，车灯划过水面")

    fresh = client.get(f"{API}/projects/{pid}/scenes/{sid}")
    assert fresh.status_code == 200, fresh.text
    scene = fresh.json()
    assert scene["prompt_ok"] is True, "写了 prompt 就该是完整的"
    assert scene["cast"] == [] and scene["locations"] == [], "人物 / 地点默认一个都不选"
    assert scene["node_limit"] == 9

    cast = client.put(f"{API}/projects/{pid}/scenes/{sid}/cast", json={"appearance_ids": [app_id]})
    assert cast.status_code == 200, cast.text
    assert [c["character_name"] for c in cast.json()["cast"]] == ["林昭"]
    assert cast.json()["cast"][0]["label"], "小节点要自带一个人能看懂的名字"

    # 多选地点：第一条同时是主地点，会同步进 Scene.location_variant_id
    locs = client.put(
        f"{API}/projects/{pid}/scenes/{sid}/locations", json={"location_variant_ids": [v2, v1]}
    )
    assert locs.status_code == 200, locs.text
    body = locs.json()
    assert [row["location_variant_id"] for row in body["locations"]] == [v2, v1]
    assert [row["is_primary"] for row in body["locations"]] == [True, False]
    assert body["location_variant_id"] == v2, "主地点必须与列表第一条一致"

    # 清空：不选地点了，主地点也要跟着清掉，不能留一个指向已移除节点的列
    cleared = client.put(
        f"{API}/projects/{pid}/scenes/{sid}/locations", json={"location_variant_ids": []}
    )
    assert cleared.json()["locations"] == []
    assert cleared.json()["location_variant_id"] is None
    assert (
        client.put(f"{API}/projects/{pid}/scenes/{sid}/cast", json={"appearance_ids": []}).json()[
            "cast"
        ]
        == []
    )


def test_primary_location_patch_keeps_list_in_sync(client: TestClient, pid: str) -> None:
    """从工作台里换主地点（PATCH 一个列）时，地点列表不能和那一列各说一套。"""
    v1 = make_variant(client, pid, "城南旧宅", "雨夜")
    v2 = make_variant(client, pid, "天台", "黄昏")
    sid = make_scene(client, pid, "雨夜追车", location_variant_id=v1)
    assert [r["location_variant_id"] for r in node_of(client, pid, sid)["locations"]] == [v1]

    swapped = client.patch(f"{API}/projects/{pid}/scenes/{sid}", json={"location_variant_id": v2})
    rows = swapped.json()["locations"]
    assert [r["location_variant_id"] for r in rows] == [v2, v1], "换主地点是插到最前，不是丢掉旧的"
    assert [r["is_primary"] for r in rows] == [True, False]


# --- 2 & 3. 上限：报错要说清怎么改，改了要立刻生效 ---


def test_node_limit_is_enforced_and_says_where_to_change_it(client: TestClient, pid: str) -> None:
    sid = make_scene(client, pid, "群戏", prompt="十个人挤在一间屋子里")
    ids = [make_appearance(client, pid, f"角色{i}", sheet=False) for i in range(10)]

    resp = client.put(f"{API}/projects/{pid}/scenes/{sid}/cast", json={"appearance_ids": ids})
    assert resp.status_code == 422
    err = error_of(resp)
    assert err["code"] == "VALIDATION_ERROR"
    assert "9" in err["detail"], "错误里要写出当前上限是多少"
    assert any("scene.node_limit" in s for s in err["suggestions"]), "必须告诉用户上限在哪儿改"
    assert node_of(client, pid, sid)["cast"] == [], "被拒的那一次不能留下半份数据"

    # 刚好到上限是允许的
    ok = client.put(f"{API}/projects/{pid}/scenes/{sid}/cast", json={"appearance_ids": ids[:9]})
    assert ok.status_code == 200, ok.text
    assert len(ok.json()["cast"]) == 9


def test_node_limit_is_configurable_at_runtime(client: TestClient, pid: str) -> None:
    """上限是应用级设置里的一项：改完立刻生效，并且节点自己带着当前上限。"""
    sid = make_scene(client, pid, "群戏", prompt="十个人挤在一间屋子里")
    ids = [make_appearance(client, pid, f"角色{i}", sheet=False) for i in range(10)]

    patched = client.patch(f"{API}/settings", json={"scene.node_limit": 10})
    assert patched.status_code == 200, patched.text
    field = next(f for f in patched.json()["fields"] if f["key"] == "scene.node_limit")
    assert field["value"] == 10 and field["source"] == "file"

    ok = client.put(f"{API}/projects/{pid}/scenes/{sid}/cast", json={"appearance_ids": ids})
    assert ok.status_code == 200, ok.text
    assert len(ok.json()["cast"]) == 10
    assert node_of(client, pid, sid)["node_limit"] == 10, "节点要带着当前上限，前端不写死 9"

    bad = client.patch(f"{API}/settings", json={"scene.node_limit": 0})
    assert bad.status_code == 422
    error_of(bad)


# --- 1. prompt 兜底：三处口径必须一致 ---


def test_scene_prompt_falls_back_for_shots(
    client: TestClient, pid: str, video_preset: str
) -> None:
    """最后那一步要真入队，所以 `video_preset` 把默认那条路的预设摆好（入队门槛在
    `services/route.py::require()`）——这里测的是 prompt 兜底，不是那道门槛。"""
    variant = make_variant(client, pid, "城南旧宅", "雨夜")
    app_id = make_appearance(client, pid, "林昭")
    sid = make_scene(client, pid, "雨夜追车", location_variant_id=variant)
    shot = make_shot(client, pid, sid, "车灯划过水面")
    client.put(f"{API}/projects/{pid}/shots/{shot}/cast", json={"appearance_ids": [app_id]})

    ctx = client.get(f"{API}/projects/{pid}/shots/{shot}/context").json()
    assert ctx["complete"] is False
    assert any("prompt" in p for p in ctx["problems"])

    assert (
        client.patch(
            f"{API}/projects/{pid}/scenes/{sid}", json={"prompt": "雨夜追车，车灯划水"}
        ).status_code
        == 200
    )
    ctx = client.get(f"{API}/projects/{pid}/shots/{shot}/context").json()
    assert ctx["complete"] is True, "幕级 prompt 要能兜底镜头级的空 prompt"

    client.post(f"{API}/projects/{pid}/queue/pause")
    job = client.post(f"{API}/projects/{pid}/shots/{shot}/generate", json={})
    assert job.status_code == 201, job.text
    queued = client.get(f"{API}/projects/{pid}/jobs").json()
    mine = next(j for j in queued if j["id"] == job.json()["id"])
    assert mine["params"]["prompt"] == "雨夜追车，车灯划水", "入队参数要用的是同一个兜底口径"


def test_scene_cast_is_inherited_by_shots_without_their_own(client: TestClient, pid: str) -> None:
    """人物小节点必须真的影响生成：镜头没挂自己的出场表时用这一幕的。"""
    variant = make_variant(client, pid, "城南旧宅", "雨夜")
    app_id = make_appearance(client, pid, "林昭")
    sid = make_scene(client, pid, "雨夜追车", location_variant_id=variant, prompt="雨夜追车")
    shot = make_shot(client, pid, sid, "车灯划过水面")

    ctx = client.get(f"{API}/projects/{pid}/shots/{shot}/context").json()
    assert any("角色" in p for p in ctx["problems"]), "谁都没挂的时候要说出来"

    client.put(f"{API}/projects/{pid}/scenes/{sid}/cast", json={"appearance_ids": [app_id]})
    ctx = client.get(f"{API}/projects/{pid}/shots/{shot}/context").json()
    sheet = next(i for i in ctx["items"] if i["kind"] == "character_sheet")
    assert sheet["included"] is True
    assert "本幕人物" in sheet["label"], "继承来的要标出来，不能让人以为是镜头自己挂的"
    assert ctx["complete"] is True


# --- 4. 每个镜头采用了哪一段（幕上没有「主视频」这种东西） ---


def test_node_says_no_video_before_any_generation(client: TestClient, pid: str) -> None:
    sid = make_scene(client, pid, "雨夜追车", prompt="雨夜追车")
    shot = make_shot(client, pid, sid, "车灯划过水面")
    node = node_of(client, pid, sid)
    assert node["has_video"] is False
    assert node["video_path"] is None and node["video_count"] == 0
    assert node["video_shot_id"] is None and node["video_adopted"] is False

    listed = client.get(f"{API}/projects/{pid}/scenes/{sid}/videos")
    assert listed.status_code == 200, listed.text
    body = listed.json()
    # 镜头照旧列出来（空的那一组也要有），否则界面上看不出「这个镜头还没出片」
    assert [g["shot_id"] for g in body["shots"]] == [shot]
    assert body["shots"][0]["items"] == [] and body["shots"][0]["adopted_version_id"] is None
    assert (body["total"], body["adopted_count"]) == (0, 0)
    assert body["note"], "空列表也要给一句解释"


def test_node_plays_video_and_never_feeds_mp4_to_an_img(client: TestClient, pid: str) -> None:
    sid = make_scene(client, pid, "雨夜追车", prompt="雨夜追车")
    shot = make_shot(client, pid, sid, "车灯划过水面")
    made = client.post(
        f"{API}/projects/{pid}/shots/{shot}/versions",
        json={"asset_id": upload_mp4(client, pid, "v1.mp4"), "duration": 4.0},
    )
    assert made.status_code == 201, made.text

    node = node_of(client, pid, sid)
    assert node["has_video"] is True and node["video_count"] == 1
    assert str(node["video_path"]).endswith(".mp4")
    assert node["video_version_id"] == made.json()["id"]
    assert node["video_shot_id"] == shot, "采用是镜头级的，所以要知道播的这段属于哪个镜头"
    assert node["video_adopted"] is True, "新版本自动成为当前版本，所以播的就是采用的那一段"
    assert node["thumbnail_path"] is None, "缩略图只认图片，绝不能把 mp4 塞给 <img>"

    # 同一幕里再来一张图片版本：它可以当封面，但不能变成「能播的那一段」
    image_shot = make_shot(client, pid, sid, "分镜草图")
    client.post(
        f"{API}/projects/{pid}/shots/{image_shot}/versions",
        json={"asset_id": upload_png(client, pid, "generated_image", "draft.png"), "kind": "image"},
    )
    node = node_of(client, pid, sid)
    assert node["video_count"] == 1, "图片版本不算可播的视频"
    assert str(node["thumbnail_path"]).endswith(".png")
    assert str(node["video_path"]).endswith(".mp4")


def test_node_marks_the_played_clip_as_not_adopted(client: TestClient, pid: str) -> None:
    """播的那一段不是所属镜头采用的那一版时，必须标出来而不是假装是。

    这种情形是真会发生的：这个镜头最后采用的是一张图（T2I 的分镜草图），
    但它先前生成过视频。节点上仍然播那段视频（已经出片了却看不见更糟），
    只是 `video_adopted=false`，界面上说清「播的只是自动挑的一段」。
    """
    sid = make_scene(client, pid, "雨夜追车", prompt="雨夜追车")
    shot = make_shot(client, pid, sid, "车灯划过水面")
    clip = client.post(
        f"{API}/projects/{pid}/shots/{shot}/versions",
        json={"asset_id": upload_mp4(client, pid, "v1.mp4"), "duration": 4.0},
    ).json()
    picture = client.post(
        f"{API}/projects/{pid}/shots/{shot}/versions",
        json={"asset_id": upload_png(client, pid, "generated_image", "draft.png"), "kind": "image"},
    ).json()

    node = node_of(client, pid, sid)
    assert node["video_version_id"] == clip["id"], "图片当不了「能播的那一段」"
    assert node["video_adopted"] is False

    listed = client.get(f"{API}/projects/{pid}/scenes/{sid}/videos").json()
    group = listed["shots"][0]
    assert group["adopted_version_id"] == picture["id"]
    assert [row["id"] for row in group["items"]] == [clip["id"]]
    assert [row["reason"] for row in group["omitted"]], "不能当候选的要说清为什么"


def test_adopting_a_shot_video_switches_that_shots_current_version(
    client: TestClient, pid: str
) -> None:
    """采用走的是全工程唯一那个入口：`POST /versions/{id}/current`。

    幕上刻意没有第二个「主视频」指针——一幕下面有很多镜头，每个镜头各自生成很多段，
    「用哪一段」只能一个镜头一个镜头地定，而时间线装配认的就是 `Shot.current_version_id`。
    """
    sid = make_scene(client, pid, "雨夜追车", prompt="雨夜追车")
    shot = make_shot(client, pid, sid, "车灯划过水面")
    first = client.post(
        f"{API}/projects/{pid}/shots/{shot}/versions",
        json={"asset_id": upload_mp4(client, pid, "v1.mp4")},
    ).json()
    second = client.post(
        f"{API}/projects/{pid}/shots/{shot}/versions",
        json={"asset_id": upload_mp4(client, pid, "v2.mp4")},
    ).json()

    listed = client.get(f"{API}/projects/{pid}/scenes/{sid}/videos").json()
    group = listed["shots"][0]
    assert (group["shot_id"], group["index_no"], group["kind"]) == (shot, 1, "shot")
    # 同一镜头内新版本在前，和 `GET /shots/{id}/versions` 同一个口径
    assert [row["id"] for row in group["items"]] == [second["id"], first["id"]]
    assert [row["version_no"] for row in group["items"]] == [2, 1]
    assert group["adopted_version_id"] == second["id"], "新版本自动成为当前版本"
    assert [row["is_adopted"] for row in group["items"]] == [True, False]
    assert (listed["total"], listed["adopted_count"]) == (2, 1)

    adopted = client.post(f"{API}/projects/{pid}/versions/{first['id']}/current")
    assert adopted.status_code == 200, adopted.text

    again = client.get(f"{API}/projects/{pid}/scenes/{sid}/videos").json()["shots"][0]
    assert again["adopted_version_id"] == first["id"]
    assert [row["is_adopted"] for row in again["items"]] == [False, True]
    versions = client.get(f"{API}/projects/{pid}/shots/{shot}/versions").json()
    assert {v["id"]: v["is_current"] for v in versions}[first["id"]] is True
    assert len(versions) == 2, "版本只增不改，采用不会删掉任何一条"
    # 节点上播的也要跟着换：流程图播这一段、时间线导出另一段是不能接受的
    node = node_of(client, pid, sid)
    assert node["video_version_id"] == first["id"] and node["video_adopted"] is True


def test_timeline_assembles_the_adopted_video_of_each_shot(client: TestClient, pid: str) -> None:
    """采用哪一段，时间线就装配哪一段——这才是「采用」的意义所在。"""
    sid = make_scene(client, pid, "雨夜追车", prompt="雨夜追车")
    shot = make_shot(client, pid, sid, "车灯划过水面")
    first = client.post(
        f"{API}/projects/{pid}/shots/{shot}/versions",
        json={"asset_id": upload_mp4(client, pid, "v1.mp4"), "duration": 3.0},
    ).json()
    client.post(
        f"{API}/projects/{pid}/shots/{shot}/versions",
        json={"asset_id": upload_mp4(client, pid, "v2.mp4"), "duration": 5.0},
    )

    def assemble() -> dict[str, Any]:
        resp = client.post(f"{API}/projects/{pid}/timeline/assemble", json={"replace": True})
        assert resp.status_code == 200, resp.text
        track = next(t for t in resp.json()["timeline"]["tracks"] if t["kind"] == "video")
        assert len(track["clips"]) == 1
        return dict(track["clips"][0])

    assert assemble()["version_no"] == 2, "默认用的就是这个镜头当前采用的那一版"

    client.post(f"{API}/projects/{pid}/versions/{first['id']}/current")
    clip = assemble()
    assert (clip["version_no"], clip["duration"]) == (1, 3.0), "换了采用，装配出来的就得换"


def test_adopting_a_version_that_does_not_exist_is_a_four_element_error(
    client: TestClient, pid: str
) -> None:
    missing = client.post(f"{API}/projects/{pid}/versions/ver_不存在/current")
    assert missing.status_code == 404
    error_of(missing)


def test_the_adopted_clip_follows_the_shot_when_it_moves(client: TestClient, pid: str) -> None:
    """镜头搬去别的幕，「用哪一段」跟着它走——这正是这个指针挂在镜头上而不是幕上的原因。

    以前幕上另存一个「主视频」，镜头一搬那个指针就发霉，只能靠 issues 报「已失效」。
    现在两边不可能各说一套：从来只有一个指针。
    """
    a = make_scene(client, pid, "雨夜追车", prompt="雨夜追车")
    b = make_scene(client, pid, "天台对峙", prompt="天台对峙")
    shot = make_shot(client, pid, a, "车灯划过水面")
    version = client.post(
        f"{API}/projects/{pid}/shots/{shot}/versions",
        json={"asset_id": upload_mp4(client, pid, "v1.mp4"), "duration": 4.0},
    ).json()

    moved = client.post(f"{API}/projects/{pid}/shots/{shot}/move", json={"scene_id": b})
    assert moved.status_code == 200, moved.text

    left = node_of(client, pid, a)
    assert left["has_video"] is False and left["video_count"] == 0
    assert not any("视频" in i for i in left["issues"]), "没有会发霉的指针，就没有「已失效」可报"

    arrived = node_of(client, pid, b)
    assert arrived["video_version_id"] == version["id"]
    assert arrived["video_shot_id"] == shot and arrived["video_adopted"] is True
    group = client.get(f"{API}/projects/{pid}/scenes/{b}/videos").json()["shots"][0]
    assert group["adopted_version_id"] == version["id"]


# --- 5. 挑人物 / 地点时看得到图 ---


def test_small_nodes_and_pick_lists_carry_thumbnails(client: TestClient, pid: str) -> None:
    """挂上去的小节点与「可挑什么」的清单都要带图，且**只带图片**。

    挑人物 / 地点是看图的活：只给名字，用户得先去角色页翻一遍才知道哪个是哪个。
    图的路径由后端给（相对工程目录），前端过 `fileUrl` 拼 URL，不自己查资产总账。
    """
    variant = make_variant(client, pid, "城南旧宅", "雨夜")
    with_sheet = make_appearance(client, pid, "林昭")
    no_image = make_appearance(client, pid, "阿岚", sheet=False)
    # 角色表挂了一段视频：有版本，但不能当 <img src>
    client.post(
        f"{API}/projects/{pid}/appearances/{no_image}/sheets",
        json={"asset_id": upload_mp4(client, pid, "turnaround.mp4")},
    )
    sid = make_scene(client, pid, "雨夜追车", prompt="雨夜追车")
    client.put(
        f"{API}/projects/{pid}/scenes/{sid}/cast", json={"appearance_ids": [with_sheet, no_image]}
    )
    client.put(
        f"{API}/projects/{pid}/scenes/{sid}/locations", json={"location_variant_ids": [variant]}
    )

    node = node_of(client, pid, sid)
    assert str(node["cast"][0]["thumbnail_path"]).endswith(".png")
    assert node["cast"][1]["thumbnail_path"] is None, "缩略图只认图片，mp4 不算"
    assert str(node["locations"][0]["thumbnail_path"]).endswith(".png")

    options = client.get(f"{API}/projects/{pid}/scene-node-options")
    assert options.status_code == 200, options.text
    body = options.json()
    assert body["node_limit"] == 9
    assert "scene.node_limit" in body["limit_hint"], "上限怎么改只有一处口径"

    rows = {row["appearance_id"]: row for row in body["cast"]}
    assert set(rows) >= {with_sheet, no_image}
    assert "林昭" in rows[with_sheet]["label"]
    assert str(rows[with_sheet]["thumbnail_path"]).endswith(".png")
    assert rows[with_sheet]["has_sheet"] is True
    # 有角色表但那份不是图：清单里照旧留着（能挂），只是没有缩略图
    assert rows[no_image]["has_sheet"] is True
    assert rows[no_image]["thumbnail_path"] is None

    picked = {row["id"]: row for row in body["locations"]}
    assert picked[variant]["label"] == "城南旧宅 · 雨夜"
    assert str(picked[variant]["thumbnail_path"]).endswith(".png")

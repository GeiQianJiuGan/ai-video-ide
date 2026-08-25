"""Step 7-9 验收：生成队列与版本、时间线与导出、概览与连续性。

四个不变量在这里被钉住：
  1. 版本只增不改——手动导入也走版本系统，切回旧版本不删任何东西；
  2. 队列里的等待必须能解释——「等上游末帧」要写清等谁；
  3. 时间线完全不依赖 AI——装配/剪辑/撤销/导出预检在 ComfyUI 与 LLM 都缺席时照常跑；
  4. 缺 FFmpeg、缺素材、空时间线都必须是带修复建议的结构化错误，绝不静默失败。

队列在每次入队前先暂停：进程内 pump 一看到暂停就立刻返回，因此没有任何一条测试
会真的去连 ComfyUI。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core import ffmpeg as ffmpeg_tool
from tests.conftest import error_of, ready_workflow, upload_png

# --- 公共脚手架 ---


def pause(client: TestClient, pid: str) -> dict[str, Any]:
    """入队前必须先暂停，否则 pump 会真的去敲 ComfyUI。"""
    resp = client.post(f"/api/v1/projects/{pid}/queue/pause")
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


def make_scene(client: TestClient, pid: str, title: str = "第一场", **patch: Any) -> dict[str, Any]:
    resp = client.post(f"/api/v1/projects/{pid}/scenes", json={"title": title, **patch})
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


def make_shot(client: TestClient, pid: str, sid: str, **patch: Any) -> dict[str, Any]:
    resp = client.post(f"/api/v1/projects/{pid}/scenes/{sid}/shots", json=patch)
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


def complete_shot(client: TestClient, pid: str) -> dict[str, Any]:
    """凑一个上下文完整的镜头：地点变体 + 有角色表的形象 + prompt。"""
    loc = client.post(f"/api/v1/projects/{pid}/locations", json={"name": "城南旧宅"}).json()
    variant = client.post(
        f"/api/v1/projects/{pid}/locations/{loc['id']}/variants",
        json={"name": "雨夜", "time_of_day": "夜"},
    ).json()
    client.post(
        f"/api/v1/projects/{pid}/variants/{variant['id']}/references",
        json={"asset_id": upload_png(client, pid, "location_reference", "loc.png")},
    )
    char = client.post(f"/api/v1/projects/{pid}/characters", json={"name": "林昭"}).json()
    apps = client.get(f"/api/v1/projects/{pid}/characters/{char['id']}/appearances").json()
    app_id = apps[0]["id"]
    client.post(
        f"/api/v1/projects/{pid}/appearances/{app_id}/sheets",
        json={"asset_id": upload_png(client, pid, "character_sheet", "sheet.png")},
    )
    scene = make_scene(client, pid, "第一场", location_variant_id=variant["id"])
    shot = make_shot(client, pid, scene["id"], title="推近", prompt="雨夜，林昭推门")
    client.put(f"/api/v1/projects/{pid}/shots/{shot['id']}/cast", json={"appearance_ids": [app_id]})
    return {"scene": scene, "shot": shot, "variant": variant, "appearance_id": app_id}


def add_version(
    client: TestClient, pid: str, shot_id: str, *, name: str, duration: float | None = None
) -> dict[str, Any]:
    """手动导入一版成片：不生成也要能把工程做完。"""
    resp = client.post(
        f"/api/v1/projects/{pid}/shots/{shot_id}/versions",
        json={
            "asset_id": upload_png(client, pid, "generated_video", name),
            "kind": "video",
            "duration": duration,
        },
    )
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


# --- Step 7：版本（只增不改） ---


def test_manual_version_is_appended_and_never_overwritten(client: TestClient, pid: str) -> None:
    scene = make_scene(client, pid)
    shot = make_shot(client, pid, scene["id"])
    v1 = add_version(client, pid, shot["id"], name="v1.png", duration=3.0)
    v2 = add_version(client, pid, shot["id"], name="v2.png", duration=5.0)
    assert (v1["version_no"], v2["version_no"]) == (1, 2)
    assert v1["source"] == "manual"

    listed = client.get(f"/api/v1/projects/{pid}/shots/{shot['id']}/versions").json()
    assert [v["version_no"] for v in listed] == [2, 1]
    assert [v["is_current"] for v in listed] == [True, False]

    fresh = client.get(f"/api/v1/projects/{pid}/shots/{shot['id']}").json()
    assert fresh["current_version_id"] == v2["id"]
    assert fresh["status"] == "generated"

    back = client.post(f"/api/v1/projects/{pid}/versions/{v1['id']}/current")
    assert back.status_code == 200, back.text
    listed = client.get(f"/api/v1/projects/{pid}/shots/{shot['id']}/versions").json()
    assert [v["is_current"] for v in listed] == [False, True], "切回旧版本不该删掉新版本"
    assert len(listed) == 2


def test_version_of_unknown_shot_is_a_structured_404(client: TestClient, pid: str) -> None:
    resp = client.get(f"/api/v1/projects/{pid}/shots/sht_nope/versions")
    assert resp.status_code == 404
    assert "镜头" in error_of(resp)["title"]


# --- Step 7：入队门槛 ---


def use_legacy_workflow_path(client: TestClient) -> None:
    """把调用方式切回旧的节点绑定路径（兼容选项）。

    默认路径是生成适配层，它不需要工作流——「没绑工作流」不再是入队门槛。
    只有这条兼容路径仍然要求先有一份已校验的工作流，所以要测它就得先显式选它。
    """
    resp = client.patch("/api/v1/settings", json={"video.provider": "comfy_workflow"})
    assert resp.status_code == 200, resp.text


def test_generate_without_a_workflow_names_the_missing_capability(
    client: TestClient, pid: str
) -> None:
    pause(client, pid)
    use_legacy_workflow_path(client)
    scene = make_scene(client, pid)
    shot = make_shot(client, pid, scene["id"])
    resp = client.post(f"/api/v1/projects/{pid}/shots/{shot['id']}/generate", json={})
    assert resp.status_code == 400
    err = error_of(resp)
    assert err["code"] == "MISSING_CAPABILITY"
    assert "image2video" in err["title"]
    assert "图生视频" in err["detail"], "能力不可用必须说出影响，而不是只说名字"
    assert err["related_ids"]["capability"] == "image2video"


def test_default_path_needs_no_workflow_but_still_gates_on_context(
    client: TestClient, pid: str
) -> None:
    """默认走适配层：门槛从「有没有绑工作流」变成「上下文齐不齐」。"""
    pause(client, pid)
    scene = make_scene(client, pid)
    shot = make_shot(client, pid, scene["id"])
    resp = client.post(f"/api/v1/projects/{pid}/shots/{shot['id']}/generate", json={})
    assert resp.status_code == 400
    assert error_of(resp)["code"] == "CONTEXT_INCOMPLETE"

    forced = client.post(
        f"/api/v1/projects/{pid}/shots/{shot['id']}/generate", json={"check_context": False}
    )
    assert forced.status_code == 201, forced.text
    assert forced.json()["workflow_id"] is None, "适配层路径不该给任务塞一个工作流"


def test_generate_gate_on_incomplete_context_can_be_bypassed(client: TestClient, pid: str) -> None:
    pause(client, pid)
    ready_workflow(client, pid, "image2video")
    scene = make_scene(client, pid)
    shot = make_shot(client, pid, scene["id"])

    resp = client.post(f"/api/v1/projects/{pid}/shots/{shot['id']}/generate", json={})
    assert resp.status_code == 400
    err = error_of(resp)
    assert err["code"] == "CONTEXT_INCOMPLETE"
    assert "地点变体" in err["detail"]
    assert any("手动添加" in s for s in err["suggestions"]), "必须留一条「我确认无误」的出口"

    forced = client.post(
        f"/api/v1/projects/{pid}/shots/{shot['id']}/generate", json={"check_context": False}
    )
    assert forced.status_code == 201, forced.text
    assert forced.json()["status"] == "queued"


def test_complete_context_enqueues_with_a_frozen_snapshot(client: TestClient, pid: str) -> None:
    pause(client, pid)
    ready_workflow(client, pid, "image2video")
    made = complete_shot(client, pid)
    resp = client.post(f"/api/v1/projects/{pid}/shots/{made['shot']['id']}/generate", json={})
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["kind"] == "image2video"
    assert job["status"] == "queued"

    jobs = client.get(f"/api/v1/projects/{pid}/jobs").json()
    assert [j["id"] for j in jobs] == [job["id"]]
    assert jobs[0]["shot_index_no"] == 1
    assert jobs[0]["params"]["prompt"] == "雨夜，林昭推门"
    # 当次上下文被冻结进任务，事后改角色表不会悄悄改变这次生成的输入
    assert len(jobs[0]["params"]["context"]["included"]) == 2


def test_upstream_dependency_waits_with_an_explicit_reason(client: TestClient, pid: str) -> None:
    pause(client, pid)
    ready_workflow(client, pid, "first_last_frame")
    scene = make_scene(client, pid)
    first = make_shot(client, pid, scene["id"], title="推近")
    second = make_shot(client, pid, scene["id"], title="拉远")
    linked = client.patch(
        f"/api/v1/projects/{pid}/shots/{second['id']}", json={"prev_shot_id": first["id"]}
    )
    assert linked.status_code == 200, linked.text

    resp = client.post(
        f"/api/v1/projects/{pid}/shots/{second['id']}/generate", json={"check_context": False}
    )
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["kind"] == "first_last_frame", "有上游时默认走首尾帧"
    assert job["status"] == "waiting"
    assert job["depends_on"] == first["id"]
    assert job["wait_reason"] == "等待上游 Shot 1 完成（需要末帧）"

    state = client.get(f"/api/v1/projects/{pid}/queue").json()
    assert state["paused"] is True
    assert state["counts"] == {"waiting": 1}
    assert state["active"] == 1


def test_scene_generate_reports_every_skipped_shot(client: TestClient, pid: str) -> None:
    pause(client, pid)
    use_legacy_workflow_path(client)
    scene = make_scene(client, pid)
    make_shot(client, pid, scene["id"], title="A")
    make_shot(client, pid, scene["id"], title="B")

    resp = client.post(f"/api/v1/projects/{pid}/scenes/{scene['id']}/generate", json={})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert (body["queued"], body["total"]) == ([], 2)
    assert [s["index_no"] for s in body["skipped"]] == [1, 2]
    assert body["skipped"][0]["error"]["code"] == "MISSING_CAPABILITY"
    assert body["skipped"][0]["error"]["suggestions"]

    empty = make_scene(client, pid, "空场")
    resp = client.post(f"/api/v1/projects/{pid}/scenes/{empty['id']}/generate", json={})
    assert resp.status_code == 422
    assert "还没有镜头" in error_of(resp)["title"]


def enqueue(client: TestClient, pid: str, shot_id: str, **body: Any) -> dict[str, Any]:
    resp = client.post(
        f"/api/v1/projects/{pid}/shots/{shot_id}/generate", json={"check_context": False, **body}
    )
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


def test_cancel_and_retry_state_machine(client: TestClient, pid: str) -> None:
    pause(client, pid)
    ready_workflow(client, pid, "image2video")
    scene = make_scene(client, pid)
    shot = make_shot(client, pid, scene["id"])
    job = enqueue(client, pid, shot["id"])

    canceled = client.post(f"/api/v1/projects/{pid}/jobs/{job['id']}/cancel")
    assert canceled.status_code == 200, canceled.text
    assert canceled.json()["status"] == "canceled"

    again = client.post(f"/api/v1/projects/{pid}/jobs/{job['id']}/cancel")
    assert again.status_code == 409
    err = error_of(again)
    assert err["code"] == "CONFLICT"
    assert "已经结束" in err["title"]

    retried = client.post(f"/api/v1/projects/{pid}/jobs/{job['id']}/retry")
    assert retried.status_code == 200, retried.text
    assert retried.json()["status"] == "queued"

    resp = client.post(f"/api/v1/projects/{pid}/jobs/{job['id']}/retry")
    assert resp.status_code == 409
    assert "只有失败或已取消" in error_of(resp)["title"]

    resp = client.post(f"/api/v1/projects/{pid}/jobs/job_nope/cancel")
    assert resp.status_code == 404
    assert "任务" in error_of(resp)["title"]


def test_priority_reorders_the_queue_and_resume_clears_the_pause(
    client: TestClient, pid: str
) -> None:
    pause(client, pid)
    ready_workflow(client, pid, "image2video")
    scene = make_scene(client, pid)
    low = enqueue(client, pid, make_shot(client, pid, scene["id"], title="A")["id"])
    high = enqueue(client, pid, make_shot(client, pid, scene["id"], title="B")["id"])
    assert [j["id"] for j in client.get(f"/api/v1/projects/{pid}/jobs").json()] == [
        low["id"],
        high["id"],
    ]

    bumped = client.put(
        f"/api/v1/projects/{pid}/jobs/{high['id']}/priority", json={"priority": 500}
    )
    assert bumped.status_code == 200, bumped.text
    assert bumped.json()["priority"] == 500
    assert [j["id"] for j in client.get(f"/api/v1/projects/{pid}/jobs").json()] == [
        high["id"],
        low["id"],
    ]
    assert len(client.get(f"/api/v1/projects/{pid}/jobs?status=queued").json()) == 2

    # 队列清空后再 resume：pump 没活干，不会去碰 ComfyUI
    for job in (low, high):
        client.post(f"/api/v1/projects/{pid}/jobs/{job['id']}/cancel")
    resumed = client.post(f"/api/v1/projects/{pid}/queue/resume")
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["paused"] is False
    assert resumed.json()["active"] == 0
    assert client.post(f"/api/v1/projects/{pid}/queue/retry-failed").json() == {"retried": []}


def test_cancel_all_and_clear_failed_and_delete_job(client: TestClient, pid: str) -> None:
    pause(client, pid)
    ready_workflow(client, pid, "image2video")
    scene = make_scene(client, pid)
    s1 = make_shot(client, pid, scene["id"], title="S1")
    s2 = make_shot(client, pid, scene["id"], title="S2")
    j1 = enqueue(client, pid, s1["id"])
    j2 = enqueue(client, pid, s2["id"])

    # 1. 一键取消全部活跃任务
    res = client.post(f"/api/v1/projects/{pid}/queue/cancel-all")
    assert res.status_code == 200, res.text
    assert res.json()["count"] == 2
    assert set(res.json()["cancelled"]) == {j1["id"], j2["id"]}

    # 2. 删除单条已取消任务
    del_res = client.delete(f"/api/v1/projects/{pid}/jobs/{j1['id']}")
    assert del_res.status_code == 200, del_res.text
    assert del_res.json()["deleted"] == j1["id"]

    remaining = client.get(f"/api/v1/projects/{pid}/jobs").json()
    assert len(remaining) == 1
    assert remaining[0]["id"] == j2["id"]

    # 3. 产生一个失败任务并一键清空失败记录
    j3 = enqueue(client, pid, s1["id"])
    # 模拟失败
    from app.services.base import db_of
    from app.persistence.models_gen import Job
    import asyncio
    async def set_failed():
        db = db_of(pid)
        async with db.write() as session:
            job = await session.get(Job, j3["id"])
            if job:
                job.status = "failed"
    asyncio.run(set_failed())

    clear_res = client.post(f"/api/v1/projects/{pid}/queue/clear-failed")
    assert clear_res.status_code == 200, clear_res.text
    assert clear_res.json()["cleared"] == 1

    jobs_after = client.get(f"/api/v1/projects/{pid}/jobs").json()
    assert not any(j["status"] == "failed" for j in jobs_after)


# --- Step 8：时间线 ---


def clips_of(timeline: dict[str, Any]) -> list[dict[str, Any]]:
    video = next(t for t in timeline["tracks"] if t["kind"] == "video")
    return list(video["clips"])


def assembled(client: TestClient, pid: str, *durations: float) -> dict[str, Any]:
    """按给定时长造好镜头与版本并装配，返回装配后的时间线。"""
    scene = make_scene(client, pid)
    for i, duration in enumerate(durations, start=1):
        shot = make_shot(client, pid, scene["id"], title=f"S{i}")
        add_version(client, pid, shot["id"], name=f"clip{i}.png", duration=duration)
    resp = client.post(f"/api/v1/projects/{pid}/timeline/assemble", json={"replace": True})
    assert resp.status_code == 200, resp.text
    return dict(resp.json()["timeline"])


def test_timeline_is_created_from_project_settings(client: TestClient, pid: str) -> None:
    timeline = client.get(f"/api/v1/projects/{pid}/timeline").json()
    assert timeline["id"].startswith("tml_")
    assert (timeline["width"], timeline["height"], timeline["fps"]) == (1920, 1080, 25)
    assert [t["name"] for t in timeline["tracks"]] == ["V1", "A1"]
    assert [t["kind"] for t in timeline["tracks"]] == ["video", "audio"]
    assert clips_of(timeline) == []
    assert timeline["duration_total"] == 0.0
    assert (timeline["can_undo"], timeline["can_redo"]) == (False, False)


def test_assemble_places_current_versions_and_explains_skips(client: TestClient, pid: str) -> None:
    scene = make_scene(client, pid)
    ready = make_shot(client, pid, scene["id"], title="推近")
    blank = make_shot(client, pid, scene["id"], title="空镜")
    add_version(client, pid, ready["id"], name="ready.png", duration=3.0)

    resp = client.post(f"/api/v1/projects/{pid}/timeline/assemble", json={"replace": True})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["placed"]) == 1
    assert body["skipped"] == [{"shot_id": blank["id"], "index_no": 2, "reason": "还没有当前版本"}]

    clips = clips_of(body["timeline"])
    assert len(clips) == 1
    assert (clips[0]["start"], clips[0]["duration"]) == (0.0, 3.0)
    assert clips[0]["label"] == "Shot 1 推近"
    assert clips[0]["version_no"] == 1
    assert clips[0]["missing_file"] is False
    assert clips[0]["asset_path"].startswith("generations/videos/")
    assert body["timeline"]["duration_total"] == 3.0


def test_move_snaps_to_the_neighbour_edge(client: TestClient, pid: str) -> None:
    timeline = assembled(client, pid, 3.0, 4.0)
    clips = clips_of(timeline)
    assert [c["start"] for c in clips] == [0.0, 3.0]

    moved = client.post(f"/api/v1/projects/{pid}/clips/{clips[1]['id']}/move", json={"start": 2.95})
    assert moved.status_code == 200, moved.text
    assert [c["start"] for c in clips_of(moved.json())] == [0.0, 3.0]

    # 视频轨保持连续排布：拖到前面会交换顺序并自动衔接
    front = client.post(f"/api/v1/projects/{pid}/clips/{clips[1]['id']}/move", json={"start": 0.0})
    assert [c["start"] for c in clips_of(front.json())] == [0.0, 4.0]


def test_trim_split_delete_then_undo_and_redo(client: TestClient, pid: str) -> None:
    timeline = assembled(client, pid, 3.0, 4.0)
    first, second = clips_of(timeline)

    trimmed = client.post(
        f"/api/v1/projects/{pid}/clips/{first['id']}/trim", json={"out_point": 2.0}
    )
    assert trimmed.status_code == 200, trimmed.text
    clips = clips_of(trimmed.json())
    assert [c["duration"] for c in clips] == [2.0, 4.0]
    assert [c["start"] for c in clips] == [0.0, 2.0], "ripple 裁切后不该留黑帧"

    split = client.post(f"/api/v1/projects/{pid}/clips/{second['id']}/split", json={"at": 3.0})
    assert split.status_code == 200, split.text
    clips = clips_of(split.json())
    assert [(c["start"], c["duration"]) for c in clips] == [(0.0, 2.0), (2.0, 1.0), (3.0, 3.0)]
    assert split.json()["duration_total"] == 6.0

    tail = clips[-1]
    deleted = client.delete(f"/api/v1/projects/{pid}/clips/{tail['id']}?ripple=true")
    assert deleted.status_code == 200, deleted.text
    assert [(c["start"], c["duration"]) for c in clips_of(deleted.json())] == [
        (0.0, 2.0),
        (2.0, 1.0),
    ]

    undone = client.post(f"/api/v1/projects/{pid}/timeline/undo")
    assert undone.status_code == 200, undone.text
    assert len(clips_of(undone.json())) == 3
    assert undone.json()["can_redo"] is True

    redone = client.post(f"/api/v1/projects/{pid}/timeline/redo")
    assert redone.status_code == 200, redone.text
    assert len(clips_of(redone.json())) == 2


def test_edit_errors_are_structured(client: TestClient, pid: str) -> None:
    clip = clips_of(assembled(client, pid, 3.0))[0]

    resp = client.post(
        f"/api/v1/projects/{pid}/clips/{clip['id']}/trim",
        json={"in_point": 1.0, "out_point": 1.0},
    )
    assert resp.status_code == 422
    assert "长度为零" in error_of(resp)["title"]

    resp = client.post(f"/api/v1/projects/{pid}/clips/{clip['id']}/split", json={"at": 99.0})
    assert resp.status_code == 422
    assert "切点不在片段内" in error_of(resp)["title"]

    resp = client.delete(f"/api/v1/projects/{pid}/clips/tcl_nope")
    assert resp.status_code == 404
    assert "片段" in error_of(resp)["title"]


def test_undo_on_a_fresh_timeline_says_so(client: TestClient, pid: str) -> None:
    resp = client.post(f"/api/v1/projects/{pid}/timeline/undo")
    assert resp.status_code == 409
    assert "没有可撤销的操作" in error_of(resp)["title"]

    resp = client.post(f"/api/v1/projects/{pid}/timeline/redo")
    assert resp.status_code == 409
    assert "没有可恢复的操作" in error_of(resp)["title"]


def test_replace_version_swaps_only_that_clip(client: TestClient, pid: str) -> None:
    scene = make_scene(client, pid)
    shot = make_shot(client, pid, scene["id"], title="推近")
    other = make_shot(client, pid, scene["id"], title="拉远")
    v1 = add_version(client, pid, shot["id"], name="a1.png", duration=3.0)
    add_version(client, pid, shot["id"], name="a2.png", duration=3.0)
    foreign = add_version(client, pid, other["id"], name="b1.png", duration=2.0)

    timeline = client.post(
        f"/api/v1/projects/{pid}/timeline/assemble", json={"replace": True}
    ).json()["timeline"]
    clips = clips_of(timeline)
    assert [c["version_no"] for c in clips] == [2, 1]

    swapped = client.post(
        f"/api/v1/projects/{pid}/clips/{clips[0]['id']}/version", json={"version_id": v1["id"]}
    )
    assert swapped.status_code == 200, swapped.text
    after = clips_of(swapped.json())
    assert [c["version_no"] for c in after] == [1, 1]
    assert [(c["start"], c["duration"]) for c in after] == [
        (c["start"], c["duration"]) for c in clips
    ], "换素材不该重排时间线"

    resp = client.post(
        f"/api/v1/projects/{pid}/clips/{clips[0]['id']}/version",
        json={"version_id": foreign["id"]},
    )
    assert resp.status_code == 422
    assert "版本不属于该片段的镜头" in error_of(resp)["title"]


def test_transitions_are_validated_and_removable(client: TestClient, pid: str) -> None:
    first, second = clips_of(assembled(client, pid, 3.0, 4.0))
    resp = client.post(
        f"/api/v1/projects/{pid}/transitions",
        json={
            "from_clip_id": first["id"],
            "to_clip_id": second["id"],
            "kind": "dissolve",
            "duration": 0.4,
        },
    )
    assert resp.status_code == 201, resp.text
    tid = resp.json()["id"]
    listed = client.get(f"/api/v1/projects/{pid}/transitions").json()
    assert [(t["kind"], t["duration"]) for t in listed] == [("dissolve", 0.4)]

    resp = client.post(
        f"/api/v1/projects/{pid}/transitions",
        json={"from_clip_id": first["id"], "to_clip_id": second["id"], "kind": "星形擦除"},
    )
    assert resp.status_code == 422
    err = error_of(resp)
    assert "未知的转场类型" in err["title"]
    assert "dissolve" in err["detail"]

    assert client.delete(f"/api/v1/projects/{pid}/transitions/{tid}").status_code == 204
    assert client.get(f"/api/v1/projects/{pid}/transitions").json() == []
    resp = client.delete(f"/api/v1/projects/{pid}/transitions/{tid}")
    assert resp.status_code == 404
    assert "转场" in error_of(resp)["title"]


# --- Step 8：导出预检（本机没有 FFmpeg，因此走的是缺件路径） ---


def test_export_preflight_refuses_an_empty_timeline(client: TestClient, pid: str) -> None:
    resp = client.get(f"/api/v1/projects/{pid}/export/command")
    assert resp.status_code == 422
    err = error_of(resp)
    assert "时间线是空的" in err["title"]
    assert any("自动装配" in s for s in err["suggestions"])


def test_export_preflight_names_missing_source_files_before_ffmpeg(
    client: TestClient, pid: str, project_dir: Path
) -> None:
    timeline = assembled(client, pid, 3.0)
    clip = clips_of(timeline)[0]
    source = project_dir / clip["asset_path"]
    assert source.is_file()
    source.unlink()

    resp = client.get(f"/api/v1/projects/{pid}/export/command")
    assert resp.status_code == 400
    err = error_of(resp)
    assert err["code"] == "MISSING_ASSET"
    assert "源文件不在了" in err["title"]
    assert err["related_ids"]["clip_ids"] == [clip["id"]]


def test_export_reports_the_missing_ffmpeg_and_writes_no_phantom_record(
    client: TestClient, pid: str, no_ffmpeg: None
) -> None:
    assembled(client, pid, 3.0)
    resp = client.get(f"/api/v1/projects/{pid}/export/command")
    assert resp.status_code == 400
    err = error_of(resp)
    assert err["code"] == "FFMPEG_MISSING"
    assert "找不到 FFmpeg" in err["title"]
    assert any("fetch_ffmpeg.py" in s for s in err["suggestions"]), "第一步该是拿到内置副本"
    assert any("AIVS_FFMPEG_PATH" in s for s in err["suggestions"])

    resp = client.post(f"/api/v1/projects/{pid}/export", json={})
    assert resp.status_code == 400
    assert error_of(resp)["code"] == "FFMPEG_MISSING"
    assert client.get(f"/api/v1/projects/{pid}/exports").json() == [], "预检失败不该留下导出记录"


def test_export_command_uses_the_bundled_ffmpeg(client: TestClient, pid: str) -> None:
    """内置副本在场时，预检命令里写的就是它——不去碰系统 PATH。"""
    found = ffmpeg_tool.locate("ffmpeg")
    if not found.available:
        pytest.skip("这台机器上还没有内置副本：先跑 scripts/fetch_ffmpeg.py")
    assembled(client, pid, 3.0)
    plan = client.get(f"/api/v1/projects/{pid}/export/command").json()
    assert plan["command"].startswith(found.path or "<none>")
    assert found.source in {"bundled", "path", "configured"}


def test_proxy_needs_ffmpeg_and_says_when_the_source_is_gone(
    client: TestClient, pid: str, project_dir: Path, no_ffmpeg: None
) -> None:
    asset_id = upload_png(client, pid, "generated_video", "proxy.png")
    resp = client.post(f"/api/v1/projects/{pid}/assets/{asset_id}/proxy")
    assert resp.status_code == 400
    assert error_of(resp)["code"] == "FFMPEG_MISSING"

    path = client.get(f"/api/v1/projects/{pid}/assets").json()[0]["path"]
    (project_dir / path).unlink()
    resp = client.post(f"/api/v1/projects/{pid}/assets/{asset_id}/proxy")
    assert resp.status_code == 400
    err = error_of(resp)
    assert err["code"] == "MISSING_ASSET"
    assert "源文件不在了" in err["title"]

    resp = client.post(f"/api/v1/projects/{pid}/assets/ast_nope/proxy")
    assert resp.status_code == 404
    assert "资产" in error_of(resp)["title"]


# --- Step 9：概览与连续性 ---


def test_overview_of_an_empty_project_points_nowhere(client: TestClient, pid: str) -> None:
    body = client.get(f"/api/v1/projects/{pid}/overview").json()
    assert body["project"]["id"] == pid
    assert body["counts"]["shots"] == 0
    assert body["progress"] == {"generated": 0, "total": 0, "percent": 0.0}
    assert body["shot_status"] == []
    assert body["resume"] is None
    assert body["last_export"] is None
    assert body["queue"] == {"active": 0, "failed": 0}


def test_overview_tracks_progress_and_resume_pointer(client: TestClient, pid: str) -> None:
    scene = make_scene(client, pid)
    done = make_shot(client, pid, scene["id"], title="推近", duration=3.0)
    make_shot(client, pid, scene["id"], title="空镜", duration=5.0)
    add_version(client, pid, done["id"], name="done.png", duration=3.0)

    body = client.get(f"/api/v1/projects/{pid}/overview").json()
    assert body["counts"]["scenes"] == 1
    assert body["counts"]["shots"] == 2
    assert body["counts"]["versions"] == 1
    assert body["progress"] == {"generated": 1, "total": 2, "percent": 50.0}
    assert body["duration_total"] == 8.0
    assert {s["status"]: s["label"] for s in body["shot_status"]} == {
        "generated": "已生成",
        "draft": "草稿",
    }
    assert body["resume"]["shot_id"] == done["id"], "「继续上次工作」要指向最近改动的镜头"
    assert body["resume"]["scene_title"] == "第一场"
    assert body["resume"]["status_label"] == "已生成"


def test_activity_merges_versions_and_canceled_jobs(client: TestClient, pid: str) -> None:
    pause(client, pid)
    ready_workflow(client, pid, "image2video")
    scene = make_scene(client, pid)
    shot = make_shot(client, pid, scene["id"], title="推近")
    add_version(client, pid, shot["id"], name="act.png", duration=2.0)
    job = enqueue(client, pid, shot["id"])
    client.post(f"/api/v1/projects/{pid}/jobs/{job['id']}/cancel")

    events = client.get(f"/api/v1/projects/{pid}/overview/activity").json()
    kinds = {e["kind"] for e in events}
    assert kinds == {"version", "job_canceled"}
    assert any("产出 v1" in e["text"] for e in events)
    assert any("任务被取消" in e["text"] for e in events)
    assert len(client.get(f"/api/v1/projects/{pid}/overview/activity?limit=1").json()) == 1


def test_continuity_is_clean_on_an_empty_project(client: TestClient, pid: str) -> None:
    body = client.get(f"/api/v1/projects/{pid}/overview/continuity").json()
    assert body == {"issues": [], "counts": {"error": 0, "warning": 0, "info": 0}, "clean": True}


def test_continuity_reports_every_kind_it_can_see(client: TestClient, pid: str) -> None:
    # 一个「夜」变体，配一个自称「日」的 Scene → scene_time
    loc = client.post(f"/api/v1/projects/{pid}/locations", json={"name": "城南旧宅"}).json()
    variant = client.post(
        f"/api/v1/projects/{pid}/locations/{loc['id']}/variants",
        json={"name": "雨夜", "time_of_day": "夜"},
    ).json()
    scene = make_scene(client, pid, "第一场", location_variant_id=variant["id"], time_of_day="日")
    # 同一角色两个形象同时出场，且都没有角色表 → character_state + missing_sheet
    char = client.post(f"/api/v1/projects/{pid}/characters", json={"name": "林昭"}).json()
    root = client.get(f"/api/v1/projects/{pid}/characters/{char['id']}/appearances").json()[0]
    alt = client.post(
        f"/api/v1/projects/{pid}/characters/{char['id']}/appearances", json={"name": "雨夜版"}
    ).json()
    prop = client.post(f"/api/v1/projects/{pid}/props", json={"name": "油纸伞"}).json()

    first = make_shot(client, pid, scene["id"], title="推近")
    second = make_shot(client, pid, scene["id"], title="拉远")
    client.put(
        f"/api/v1/projects/{pid}/shots/{first['id']}/cast",
        json={"appearance_ids": [root["id"], alt["id"]]},
    )
    # 伞在 1 号镜头被丢弃，却在 2 号镜头又出场 → prop_state + missing_prop_reference
    client.put(
        f"/api/v1/projects/{pid}/shots/{first['id']}/props",
        json={"items": [{"prop_id": prop["id"], "state": "discarded"}]},
    )
    client.put(
        f"/api/v1/projects/{pid}/shots/{second['id']}/props",
        json={"items": [{"prop_id": prop["id"], "state": "present"}]},
    )
    # 2 号镜头等 1 号的末帧，而 1 号还没出片 → upstream_not_ready
    client.patch(f"/api/v1/projects/{pid}/shots/{second['id']}", json={"prev_shot_id": first["id"]})

    body = client.get(f"/api/v1/projects/{pid}/overview/continuity").json()
    assert body["clean"] is False
    kinds = [i["kind"] for i in body["issues"]]
    assert set(kinds) == {
        "character_state",
        "scene_time",
        "prop_state",
        "missing_sheet",
        "missing_prop_reference",
        "upstream_not_ready",
    }
    assert kinds[0] in ("character_state", "prop_state"), "error 必须排在前面"
    assert body["counts"] == {"error": 2, "warning": 3, "info": 2}
    for issue in body["issues"]:
        assert issue["title"] and issue["detail"] and issue["suggestions"]

    time_issue = next(i for i in body["issues"] if i["kind"] == "scene_time")
    assert "城南旧宅 · 雨夜" in time_issue["detail"]
    assert "「日」" in time_issue["detail"]
    waiting = next(i for i in body["issues"] if i["kind"] == "upstream_not_ready")
    assert waiting["title"] == "等待 Shot 1 出片"


def test_environment_states_what_is_missing_and_what_it_costs(
    client: TestClient, pid: str, no_ffmpeg: None
) -> None:
    env = client.get(f"/api/v1/projects/{pid}/overview/environment").json()
    assert "online" in env["comfy"], "ComfyUI 在不在都要有明确答复"
    assert env["ffmpeg"]["available"] is False, "内置副本与 PATH 都被掏空了"
    assert env["ffmpeg"]["impact"] == "无法导出成片；其余功能不受影响。"
    assert "fetch_ffmpeg.py" in env["ffmpeg"]["hint"], "缺了要说怎么补"
    matrix = env["capabilities"]["capabilities"]
    assert [c["capability"] for c in matrix] == [
        "text2image",
        "image2video",
        "first_last_frame",
        "upscale",
    ]
    assert all(c["ready"] is False for c in matrix)
    assert next(c for c in matrix if c["capability"] == "image2video")["impact"]
    assert "online" in env["capabilities"]["comfy"]
    assert "gpu" in env

    globally = client.get("/api/v1/environment").json()
    assert globally["capabilities"] is None, "没打开工程时不该谈能力矩阵"


def test_environment_says_which_ffmpeg_it_will_use(client: TestClient, pid: str) -> None:
    """在场时也要说清用的是哪一份：内置和「你机器上那份」排查方向不同。"""
    found = ffmpeg_tool.locate("ffmpeg")
    if not found.available:
        pytest.skip("这台机器上还没有内置副本：先跑 scripts/fetch_ffmpeg.py")
    env = client.get(f"/api/v1/projects/{pid}/overview/environment").json()["ffmpeg"]
    assert env["available"] is True
    assert env["source"] == found.source
    assert env["path"] == found.path
    assert env["impact"] is None


def test_workflow_health_lists_status_per_capability(client: TestClient, pid: str) -> None:
    assert client.get(f"/api/v1/projects/{pid}/overview/workflows").json() == []
    row = ready_workflow(client, pid, "image2video")
    health = client.get(f"/api/v1/projects/{pid}/overview/workflows").json()
    assert len(health) == 1
    assert health[0]["id"] == row["id"]
    assert health[0]["capability"] == "image2video"
    assert health[0]["status"] == "ready"
    assert health[0]["is_default"] is False, "校验通过不等于被选为默认"
    assert health[0]["validation"]["ok"] is True

    assert client.post(f"/api/v1/projects/{pid}/workflows/{row['id']}/default").status_code == 200
    assert client.get(f"/api/v1/projects/{pid}/overview/workflows").json()[0]["is_default"] is True

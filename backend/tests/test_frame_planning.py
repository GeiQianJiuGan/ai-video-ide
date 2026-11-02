"""已有镜头先审阅制帧计划，采用时原子写入且保护现有制作内容。"""

import json

from app.ai.llm import client as llm
from app.core.errors import AppError, ErrorCode
from app.services.frame_authoring import frames
from tests.test_creation import scene
from tests.test_shot_frame_slots import a_shot


def mock_planner(monkeypatch):
    calls = []
    monkeypatch.setattr(llm, "require_configured", lambda: None)

    async def complete(system, user):
        calls.append((system, json.loads(user)))
        shot = scene()["shots"][0]
        return {"frames": shot["frames"], "video_prompt": shot["video_prompt"]}

    monkeypatch.setattr(llm, "complete_json", complete)
    return calls


def test_existing_shot_plan_is_reviewed_then_applied_without_jobs_or_direct_changes(
    client, pid, monkeypatch
):
    calls = mock_planner(monkeypatch)
    sid = a_shot(client, pid)
    root = f"/api/v1/projects/{pid}"
    before = client.get(f"{root}/shots/{sid}").json()
    proposed = client.post(f"{root}/shots/{sid}/frame-plan/propose")
    assert proposed.status_code == 200, proposed.text
    plan = proposed.json()
    assert calls[0][1]["shot"]["id"] == sid
    assert "frame-video-planning" in calls[0][0]
    assert client.get(f"{root}/shots/{sid}/frames").json() == {"cards": [], "video": {}}
    response = client.post(f"{root}/shots/{sid}/frame-plan/apply", json=plan)
    assert response.status_code == 200, response.text
    ws = response.json()
    assert [c["story"] for c in ws["cards"]] == [f["story"] for f in plan["frames"]]
    assert ws["video"]["prompt"].startswith(plan["video_prompt"])
    assert ws["video"]["first_card_id"] == ws["cards"][0]["id"]
    assert ws["video"]["last_card_id"] == ws["cards"][-1]["id"]
    assert client.get(f"{root}/jobs").json() == []
    after = client.get(f"{root}/shots/{sid}").json()
    for key in (
        "description",
        "prompt",
        "intent_json",
        "current_version_id",
        "first_frame_asset_id",
    ):
        assert after.get(key) == before.get(key)
    assert client.post(f"{root}/shots/{sid}/frame-plan/apply", json=plan).status_code == 422
    assert client.get(f"{root}/shots/{sid}/frames").json() == ws


def test_plan_from_old_shot_description_is_rejected(client, pid, monkeypatch):
    mock_planner(monkeypatch)
    sid = a_shot(client, pid)
    root = f"/api/v1/projects/{pid}/shots/{sid}"
    plan = client.post(f"{root}/frame-plan/propose").json()
    client.patch(root, json={"description": "用户已更新剧情，请重新规划画面。"})
    response = client.post(f"{root}/frame-plan/apply", json=plan)
    assert response.status_code == 422
    assert client.get(f"{root}/frames").json() == {"cards": [], "video": {}}


def test_empty_frame_slots_are_filled_in_place_and_new_edits_are_protected(
    client, pid, monkeypatch
):
    mock_planner(monkeypatch)
    sid = a_shot(client, pid)
    root = f"/api/v1/projects/{pid}"
    slots = [
        client.post(f"{root}/shots/{sid}/frames", json={"title": f["title"]}).json()
        for f in scene()["shots"][0]["frames"]
    ]
    plan = client.post(f"{root}/shots/{sid}/frame-plan/propose").json()
    assert plan["frame_card_ids"] == [c["id"] for c in slots]
    client.patch(
        f"{root}/frame-cards/{slots[0]['id']}",
        json={"story": "用户已开始编辑这一画面的剧情，不能再覆盖。"},
    )
    assert client.post(f"{root}/shots/{sid}/frame-plan/apply", json=plan).status_code == 422
    client.patch(f"{root}/frame-cards/{slots[0]['id']}", json={"story": ""})
    response = client.post(f"{root}/shots/{sid}/frame-plan/apply", json=plan)
    assert response.status_code == 200, response.text
    assert [c["id"] for c in response.json()["cards"]] == [c["id"] for c in slots]
    assert all(c["story"] and c["prompt"] for c in response.json()["cards"])


def test_failed_plan_apply_rolls_back_every_frame(client, pid, monkeypatch):
    mock_planner(monkeypatch)
    sid = a_shot(client, pid)
    root = f"/api/v1/projects/{pid}/shots/{sid}"
    plan = client.post(f"{root}/frame-plan/propose").json()

    async def fail(*args, **kwargs):
        raise AppError(ErrorCode.INTERNAL, "模拟中断", "模拟独立视频草稿写入中断", ["重试采用计划"])

    monkeypatch.setattr(frames, "save_video", fail)
    response = client.post(f"{root}/frame-plan/apply", json=plan)
    assert response.status_code == 500
    assert client.get(f"{root}/frames").json() == {"cards": [], "video": {}}


def test_invalid_narrative_has_bounded_repairs_and_no_writes(client, pid, monkeypatch):
    monkeypatch.setattr(llm, "require_configured", lambda: None)
    repairs = []

    async def invalid(system, user):
        repairs.append(json.loads(user)["repair"])
        return {"frames": [], "video_prompt": ""}

    monkeypatch.setattr(llm, "complete_json", invalid)
    sid = a_shot(client, pid)
    root = f"/api/v1/projects/{pid}/shots/{sid}"
    response = client.post(f"{root}/frame-plan/propose")
    assert response.status_code == 422
    assert len(repairs) == 3 and repairs[1] and repairs[2]
    assert client.get(f"{root}/frames").json() == {"cards": [], "video": {}}

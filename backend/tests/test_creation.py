"""Complete targets, durable recovery, and atomic production writes without real LLM calls."""

import json
import time

import pytest

from app.ai.llm import client as llm
from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.persistence.models_creation import CreationRun
from app.services.story import story


def script():
    return {
        "screenplay_md": "# 雨夜来信\n少女在雨夜回家，发现父亲留下的一封信。她打开信件，读到父亲的告别，终于决定踏上旅途。结尾她迎着清晨的阳光走出房门。",
        "characters": [{"name": "阿岚", "description": "身穿蓝色外套的年轻少女"}],
        "locations": [{"name": "客厅", "description": "雨夜中有一盏暖灯的客厅"}],
        "props": [{"name": "来信", "description": "一封有旧折痕的信"}],
    }


def scene():
    return {
        "shots": [
            {
                "title": "推门",
                "description": "少女推开房门走进屋内，目光停在桌上的信件。",
                "character_names": ["阿岚"],
                "prop_names": ["来信"],
                "intent": {
                    "beat": "发现来信",
                    "shot_size": "medium shot",
                    "action": "A girl opens the door and sees a letter.",
                    "first_frame": "A girl stands by the door.",
                    "duration": 5,
                },
                "frames": [
                    {
                        "title": "开场",
                        "role": "first",
                        "story": "阿岚站在门口，手握门把，桌上的信封尚未被触碰。",
                        "prompt": "A girl stands at the doorway in a rainy evening.",
                    },
                    {
                        "title": "看见来信",
                        "role": "key",
                        "story": "阿岚的视线落在桌上的信封，身体仍停在门边。",
                        "prompt": "A girl at the door looks at an envelope on a wooden table.",
                    },
                    {
                        "title": "走近桌边",
                        "role": "last",
                        "story": "阿岚已走到桌边，凝视尚未拿起的信封，准备拆信。",
                        "prompt": "A girl stands beside a wooden table, looking at an untouched envelope.",
                    },
                ],
                "video_prompt": "The girl releases the door handle, looks at the envelope and walks to the table. Slow push-in. Sound: rain and footsteps; no dialogue or music.",
            }
        ]
    }


@pytest.fixture
def ai(client, monkeypatch):
    monkeypatch.setattr(llm, "require_configured", lambda: None)
    calls = []

    async def complete(system, user):
        data = json.loads(user)
        title = data["schema"]["title"]
        calls.append(title)
        if title == "ScriptDraft":
            return script()
        if title == "OutlineDraft":
            return {
                "scenes": [
                    {
                        "title": f"第{i}幕",
                        "location_name": "客厅",
                        "summary": "少女在这一幕发现来信并作出重要决定。",
                    }
                    for i in range(3)
                ]
            }
        return scene()

    monkeypatch.setattr(llm, "complete_json", complete)
    return calls


def wait(client, root, wanted=("review", "failed", "completed")):
    for _ in range(1000):
        result = client.get(f"{root}/director/tasks/latest")
        assert result.status_code == 200, result.text
        data = result.json()
        if data and data["status"] in wanted:
            return data
        time.sleep(0.01)
    pytest.fail("task did not finish: " + str(data))


def start(client, pid, goal="storyboard", mode="frames"):
    root = f"/api/v1/projects/{pid}"
    response = client.post(
        f"{root}/director/tasks",
        json={"message": "帮我完整创作雨夜来信的故事", "goal": goal, "mode": mode},
    )
    assert response.status_code == 201, response.text
    return root, response.json()["id"]


def test_complete_draft_has_all_scenes_and_no_empty_production_rows(client, pid, ai):
    root, rid = start(client, pid)
    row = wait(client, root)
    assert row["status"] == "review"
    assert len(row["state"]["scenes"]) == 3
    assert all(s["shots"] for s in row["state"]["scenes"])
    assert client.get(f"{root}/storyboard").json() == []
    assert ai.count("SceneDraft") == 3
    assert client.post(f"{root}/director/tasks/{rid}/apply").status_code == 200
    row = wait(client, root, ("completed", "failed"))
    assert row["status"] == "completed", row["state"]
    lanes = client.get(f"{root}/storyboard").json()
    assert [len(s["shots"]) for s in lanes] == [1, 1, 1]
    sid = lanes[0]["shots"][0]["id"]
    detail = client.get(f"{root}/shots/{sid}").json()
    assert len(detail["cast"]) == 1
    assert len(detail["props"]) == 1
    assert client.get(f"{root}/scenes").json()[0]["location_variant_id"]
    frames = client.get(f"{root}/shots/{sid}/frames").json()
    assert frames["cards"][0]["prompt"] == scene()["shots"][0]["frames"][0]["prompt"]
    assert len(frames["cards"]) == 3
    for card, expected in zip(frames["cards"], scene()["shots"][0]["frames"], strict=True):
        assert card["story"] == expected["story"]
        assert card["role"] == expected["role"]
        assert card["adopted_asset_id"] is None and card["candidates"] == []
    assert frames["video"]["prompt"].startswith(scene()["shots"][0]["video_prompt"])
    assert frames["video"]["duration"] == 5
    assert frames["video"]["first_card_id"] == frames["cards"][0]["id"]
    assert frames["video"]["last_card_id"] == frames["cards"][-1]["id"]
    assert client.get(f"{root}/jobs").json() == []
    client.post(f"{root}/director/tasks/{rid}/apply")
    assert len(client.get(f"{root}/storyboard").json()) == 3


def test_screenplay_goal_does_not_create_scenes(client, pid, ai, monkeypatch):
    monkeypatch.setattr(settings, "director_auto_apply", True)
    root, _ = start(client, pid, "screenplay", "direct")
    row = wait(client, root)
    assert row["status"] == "completed"
    assert ai == ["ScriptDraft"]
    assert client.get(f"{root}/story").json()["screenplay_md"] == script()["screenplay_md"]
    assert client.get(f"{root}/storyboard").json() == []


def test_failed_scene_write_rolls_back_and_resume_does_not_duplicate(client, pid, ai, monkeypatch):
    monkeypatch.setattr(settings, "director_auto_apply", True)
    original = story.create_shot

    async def broken(*args, **kwargs):
        raise AppError(ErrorCode.INTERNAL, "test", "injected shot failure")

    monkeypatch.setattr(story, "create_shot", broken)
    root, rid = start(client, pid)
    row = wait(client, root)
    assert row["status"] == "failed"
    assert client.get(f"{root}/storyboard").json() == []
    assert len(row["state"]["applied"]) == 4  # script checkpoint committed; scene never did
    generated = list(ai)
    monkeypatch.setattr(story, "create_shot", original)
    assert client.post(f"{root}/director/tasks/{rid}/resume").status_code == 200
    row = wait(client, root, ("completed", "failed"))
    assert row["status"] == "completed", row["state"]
    assert ai == generated  # no repeated LLM work
    assert len(client.get(f"{root}/storyboard").json()) == 3


def test_invalid_scene_is_repaired_without_showing_empty_scenes(client, pid, ai, monkeypatch):
    original = llm.complete_json
    attempts = 0

    async def repairing(system, user):
        nonlocal attempts
        if json.loads(user)["schema"]["title"] == "SceneDraft":
            attempts += 1
            if attempts == 1:
                return {"shots": []}
        return await original(system, user)

    monkeypatch.setattr(llm, "complete_json", repairing)
    root, _ = start(client, pid)
    row = wait(client, root)
    assert row["status"] == "review"
    assert len(row["state"]["attempts"]) == 1
    assert len(row["state"]["scenes"]) == 3
    assert client.get(f"{root}/storyboard").json() == []


def test_existing_user_script_is_not_overwritten_when_task_finishes(client, pid, ai, monkeypatch):
    monkeypatch.setattr(settings, "director_auto_apply", True)
    original = llm.complete_json

    async def edit_during_generation(system, user):
        result = await original(system, user)
        if json.loads(user)["schema"]["title"] == "ScriptDraft":
            await story.save_story(pid, {"screenplay_md": "用户正在编辑的剧本"})
        return result

    monkeypatch.setattr(llm, "complete_json", edit_during_generation)
    root, _ = start(client, pid, "screenplay")
    row = wait(client, root)
    assert row["status"] == "review"
    assert client.get(f"{root}/story").json()["screenplay_md"] == "用户正在编辑的剧本"


def test_pause_and_restart_resume_from_saved_scene_checkpoint(client, pid, ai, monkeypatch):
    import asyncio

    from app.services.creation import creation

    original = llm.complete_json
    count = 0

    async def waiting(system, user):
        nonlocal count
        if json.loads(user)["schema"]["title"] == "SceneDraft":
            count += 1
            if count == 2:
                await asyncio.Event().wait()
        return await original(system, user)

    monkeypatch.setattr(llm, "complete_json", waiting)
    root, rid = start(client, pid)
    for _ in range(500):
        row = client.get(f"{root}/director/tasks/{rid}").json()
        if len(row["state"]["scenes"]) == 1:
            break
        time.sleep(0.01)
    else:
        pytest.fail("first scene checkpoint was not saved")

    # Close/reopen is also how a server shutdown interrupts in-flight generation.
    async def shutdown():
        await creation.stop_project(pid)

    client.portal.call(shutdown)
    row = client.get(f"{root}/director/tasks/{rid}").json()
    assert row["status"] == "paused"
    assert len(row["state"]["scenes"]) == 1
    monkeypatch.setattr(llm, "complete_json", original)
    assert client.post(f"{root}/director/tasks/{rid}/resume").status_code == 200
    row = wait(client, root)
    assert row["status"] == "review"
    assert len(row["state"]["scenes"]) == 3
    assert ai.count("ScriptDraft") == 1 and ai.count("OutlineDraft") == 1
    assert ai.count("SceneDraft") == 3
    assert client.get(f"{root}/storyboard").json() == []


def test_duplicate_submission_reuses_completed_task_and_direct_mode_has_no_frame_cards(
    client, pid, ai, monkeypatch
):
    monkeypatch.setattr(settings, "director_auto_apply", True)
    root, rid = start(client, pid, mode="direct")
    row = wait(client, root)
    assert row["status"] == "completed", row["state"]
    generated = list(ai)
    _, again = start(client, pid, mode="direct")
    assert again == rid and ai == generated
    lanes = client.get(f"{root}/storyboard").json()
    assert len(lanes) == 3
    for lane in lanes:
        for shot in lane["shots"]:
            assert client.get(f"{root}/shots/{shot['id']}/frames").json() == {
                "cards": [],
                "video": {},
            }


@pytest.mark.parametrize("broken", ["story", "video_prompt", "first", "last"])
def test_frame_narrative_errors_are_repaired_before_review(client, pid, ai, monkeypatch, broken):
    original = llm.complete_json
    repairs = []

    async def repairing(system, user):
        data = json.loads(user)
        if data["schema"]["title"] != "SceneDraft":
            return await original(system, user)
        repairs.append(data["repair"])
        result = scene()
        if len(repairs) == 1:
            shot = result["shots"][0]
            if broken == "story":
                shot["frames"][1].pop("story")
            elif broken == "video_prompt":
                shot.pop("video_prompt")
            elif broken == "first":
                shot["frames"][1]["role"] = "first"
            else:
                shot["frames"][1]["role"] = "last"
        return result

    monkeypatch.setattr(llm, "complete_json", repairing)
    root, _ = start(client, pid)
    row = wait(client, root)
    assert row["status"] == "review", row["state"]
    assert len(row["state"]["attempts"]) == 1
    assert repairs[1]  # 把具体失败反馈给模型，而不是默默填充剧情。
    assert client.get(f"{root}/storyboard").json() == []


def test_single_opening_frame_is_valid_and_skill_is_loaded_for_frame_mode(
    client, pid, ai, monkeypatch
):
    from app.ai import skills

    original = llm.complete_json
    instructions = []

    async def single_frame(system, user):
        if json.loads(user)["schema"]["title"] == "SceneDraft":
            instructions.append(system)
            result = scene()
            result["shots"][0]["frames"] = result["shots"][0]["frames"][:1]
            return result
        return await original(system, user)

    monkeypatch.setattr(llm, "complete_json", single_frame)
    monkeypatch.setattr(settings, "director_auto_apply", True)
    root, _ = start(client, pid)
    row = wait(client, root)
    assert row["status"] == "completed", row["state"]
    assert all(skills.render("frame-video-planning") in system for system in instructions)
    for lane in client.get(f"{root}/storyboard").json():
        ws = client.get(f"{root}/shots/{lane['shots'][0]['id']}/frames").json()
        assert len(ws["cards"]) == 1
        assert ws["video"]["first_card_id"] == ws["cards"][0]["id"]
        assert ws["video"]["last_card_id"] == ""


def test_frame_workspace_failure_rolls_back_whole_scene_and_resumes(client, pid, ai, monkeypatch):
    from app.services.frame_authoring import frames

    original = frames.save_video

    async def broken(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("模拟视频草稿写入后中断")

    monkeypatch.setattr(settings, "director_auto_apply", True)
    monkeypatch.setattr(frames, "save_video", broken)
    root, rid = start(client, pid)
    row = wait(client, root)
    assert row["status"] == "failed"
    assert client.get(f"{root}/storyboard").json() == []
    generated = list(ai)
    monkeypatch.setattr(frames, "save_video", original)
    client.post(f"{root}/director/tasks/{rid}/resume")
    row = wait(client, root, ("completed", "failed"))
    assert row["status"] == "completed", row["state"]
    assert generated == ai
    lanes = client.get(f"{root}/storyboard").json()
    assert len(lanes) == 3
    for lane in lanes:
        ws = client.get(f"{root}/shots/{lane['shots'][0]['id']}/frames").json()
        assert len(ws["cards"]) == 3


@pytest.mark.asyncio
async def test_atomic_bundle_rolls_back_and_does_not_share_session_with_child_task(db):
    import asyncio

    with pytest.raises(RuntimeError):
        async with db.atomic() as parent:
            async with db.write() as session:
                session.add(CreationRun(id="crn_atomic", status="running", state_json="{}"))
            async with db.read() as session:
                assert await session.get(CreationRun, "crn_atomic") is not None

            async def child():
                async with db.read() as independent:
                    assert independent is not parent
                    assert await independent.get(CreationRun, "crn_atomic") is None

            await asyncio.create_task(child())
            raise RuntimeError("roll back complete bundle")
    async with db.read() as session:
        assert await session.get(CreationRun, "crn_atomic") is None

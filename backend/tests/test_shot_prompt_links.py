"""图形镜头编辑中的稳定链接：真实顺序、丢失输入和手动创作。"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.errors import AppError
from app.generation.mentions import resolve_request, validate_mentions
from app.generation.providers.base import Picture, RefAsset, VideoRequest, frame_seed, picture_book
from app.generation.providers.comfy_base import ComfyTasks
from app.generation.providers.http_api import _book_of
from app.generation.renderers import render_prompt
from app.services.story import story
from tests.test_shot_frame_slots import a_shot


def test_links_follow_real_feed_order_even_when_display_alias_is_picture_one():
    book = picture_book(
        {
            "hero": Picture(asset_id="hero", file="hero.png", name="女主"),
            "opening": frame_seed("first_frame", "opening.png"),
            "street": Picture(asset_id="street", file="street.png", name="街道"),
        },
        ["street", "opening", "hero"],
    )
    req = VideoRequest(
        mode="i2v", prompt="@[图片1](asset:hero) 从 @[首帧](frame:first_frame) 开始走动。"
    )
    resolve_request(req, book)
    assert req.prompt == "<Picture 3> 从 <Picture 2> 开始走动。"
    assert book.items[2].asset_id == "hero"


def test_removed_or_truncated_reference_never_turns_into_a_bare_model_tag():
    req = VideoRequest(mode="i2v", prompt="@[图片1](asset:removed)")
    with pytest.raises(AppError, match="提示词引用的素材未送入模型"):
        resolve_request(req, picture_book({}))
    with pytest.raises(AppError):
        validate_mentions(req.prompt, [{"asset_id": "removed", "over_capacity": True}])


def test_same_picture_can_be_both_first_and_last_frame():
    book = picture_book(
        {
            "first": frame_seed("first_frame", "same.png"),
            "last": frame_seed("last_frame", "same.png"),
        }
    )
    req = VideoRequest(
        mode="flf", prompt="从 @[首帧](frame:first_frame) 回到 @[尾帧](frame:last_frame)"
    )
    resolve_request(req, book)
    assert req.prompt == "从 <Picture 1> 回到 <Picture 1>"
    assert len(book.items) == 1


def test_link_survives_asset_becoming_a_frame_and_file_deduplication():
    book = picture_book(
        {
            "first": frame_seed("first_frame", "same.png"),
            "ref": Picture(asset_id="hero", file="same.png"),
        }
    )
    req = VideoRequest(mode="i2v", prompt="@[图片1](asset:hero)")
    resolve_request(req, book)
    assert req.prompt == "<Picture 1>"
    req = VideoRequest(
        mode="i2v", prompt="@[图片1](asset:hero)", frame_assets={"hero": "first_frame"}
    )
    resolve_request(req, picture_book({"first": frame_seed("first_frame", "same.png")}))
    assert req.prompt == "<Picture 1>"


def test_frame_only_comfy_workflow_receives_resolved_links():
    req = VideoRequest(
        mode="flf", prompt="从 @[首帧](frame:first_frame) 到 @[尾帧](frame:last_frame)"
    )
    book = picture_book(
        {"first": frame_seed("first_frame", "a.png"), "last": frame_seed("last_frame", "b.png")}
    )
    written = []
    ComfyTasks()._retold(req, book, "测试", lambda text: written.append(text) or True)
    assert written == ["从 <Picture 1> 到 <Picture 2>"]
    assert req.sent_prompt == written[0]
    with pytest.raises(AppError):
        ComfyTasks()._retold(
            VideoRequest(mode="flf", prompt="@[首帧](frame:first_frame)"),
            book,
            "测试",
            lambda _: False,
        )


def test_unavailable_audio_reference_is_rejected():
    ref = RefAsset(Path("voice.wav"), media="audio", asset_id="voice")
    req = VideoRequest(mode="i2v", prompt="@[音频1](asset:voice)", unavailable_assets={"voice"})
    with pytest.raises(AppError):
        resolve_request(req, picture_book({}, others=[ref]))


def test_rest_uses_the_same_stable_ids_and_contract_order(tmp_path):
    req = VideoRequest(
        mode="i2v",
        first_frame=tmp_path / "first.png",
        refs=[RefAsset(tmp_path / "hero.png", asset_id="hero")],
        prompt="@[图片1](asset:hero)",
    )
    book = _book_of(req)
    resolve_request(req, book)
    assert req.prompt == "<Picture 2>"
    assert book.items[1].to_dict()["asset_id"] == "hero"


def test_complete_manual_h3_prompt_is_not_wrapped_in_a_second_structure():
    text = "integrated_multimodal_description:\nA woman walks.\n\noverall_soundscape:\nRain.\n\nnon_diegetic_music:\nnone"
    req = VideoRequest(mode="i2v", prompt=text)
    assert render_prompt("minimax-h3", req, picture_book({})) == text


def test_manual_edit_clears_director_intent_and_empty_prompt_restores_scene(
    client: TestClient, pid: str
):
    sid = a_shot(client, pid)
    path = f"/api/v1/projects/{pid}/shots/{sid}"
    before = client.get(path).json()
    client.portal.call(
        story.update_shot, pid, sid, {"intent_json": '{"beat":"旧导演意图","action":"向左走"}'}
    )
    assert client.get(path).json()["intent"]["beat"] == "旧导演意图"
    scene_path = f"/api/v1/projects/{pid}/scenes/{before['scene_id']}"
    client.patch(scene_path, json={"params": {"prompt": "本幕参数中的雨夜"}})
    response = client.patch(path, json={"prompt": "手动写的推镜头", "manual_prompt": True})
    assert response.status_code == 200, response.text
    assert response.json()["intent"] == {}
    assert response.json()["effective_prompt"] == "手动写的推镜头"
    response = client.patch(path, json={"prompt": "", "manual_prompt": True})
    assert response.json()["effective_prompt"] == "本幕参数中的雨夜"
    assert response.json()["inherited_prompt"] == "本幕参数中的雨夜"

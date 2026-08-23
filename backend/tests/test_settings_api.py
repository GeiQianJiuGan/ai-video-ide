"""Step 1 验收：应用级设置与配置页契约。

五件事必须成立：
  1. settings.json 的覆盖压过环境变量，且每个字段说清值是哪来的（file / env / default）；
  2. API Key 永不回明文——只回 masked 与 has_value；
  3. 取值不合法当场报四要素错误，绝不悄悄存一个坏值；
  4. 探测失败也是四要素错误，而不是一个红叉；
  5. 系统提示词可改，但 JSON 输出形状那一段**永远由代码追加**——用户改不坏落库那条路。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.conftest import error_of


def fields_of(client: TestClient) -> dict[str, dict[str, Any]]:
    resp = client.get("/api/v1/settings")
    assert resp.status_code == 200, resp.text
    return {f["key"]: f for f in resp.json()["fields"]}


def test_patch_overrides_and_reports_where_the_value_came_from(client: TestClient) -> None:
    before = fields_of(client)
    assert before["video.provider"]["value"] == "comfy_preset"
    assert before["video.provider"]["source"] == "default"

    resp = client.patch(
        "/api/v1/settings",
        json={"video.provider": "http_api", "video.base_url": "http://127.0.0.1:9999/v1"},
    )
    assert resp.status_code == 200, resp.text
    after = {f["key"]: f for f in resp.json()["fields"]}
    assert after["video.provider"]["value"] == "http_api"
    assert after["video.provider"]["source"] == "file", "改过的字段要标明来自配置文件"
    assert settings.video_provider == "http_api", "覆盖必须真的生效，而不是只写进文件"

    saved = json.loads((settings.runtime_dir / "settings.json").read_text(encoding="utf-8"))
    assert saved["overrides"]["video.base_url"] == "http://127.0.0.1:9999/v1"

    # 提交 null 表示清除覆盖，回到环境变量 / 默认
    resp = client.patch("/api/v1/settings", json={"video.provider": None})
    assert resp.status_code == 200, resp.text
    assert settings.video_provider == "comfy_preset"
    assert fields_of(client)["video.provider"]["source"] == "default"


def test_settings_file_beats_env(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """环境变量是启动值，配置文件压在它上面——顺序是 文件 → 环境变量 → 默认。"""
    monkeypatch.setattr(settings, "comfy_base_url", "http://127.0.0.1:8188")
    client.patch("/api/v1/settings", json={"comfy.base_url": "http://10.0.0.9:8188"})
    assert settings.comfy_base_url == "http://10.0.0.9:8188"

    from app.generation.comfy.client import comfy

    assert comfy.base_url == "http://10.0.0.9:8188", "ComfyUI 客户端要跟着配置页走"


def test_api_key_is_never_returned_in_clear_text(client: TestClient) -> None:
    resp = client.patch("/api/v1/settings", json={"llm.api_key": "sk-abcdef123456"})
    assert resp.status_code == 200, resp.text
    field = {f["key"]: f for f in resp.json()["fields"]}["llm.api_key"]
    assert field["value"] is None
    assert field["has_value"] is True
    assert field["masked"].endswith("3456")
    assert "abcdef" not in json.dumps(resp.json()), "密钥明文绝不能出现在响应里"

    # 空串 = 清除，而不是「把密钥设成空」
    client.patch("/api/v1/settings", json={"llm.api_key": ""})
    assert fields_of(client)["llm.api_key"]["has_value"] is False


def test_bad_values_and_unknown_keys_are_structured_errors(client: TestClient) -> None:
    resp = client.patch("/api/v1/settings", json={"runtime.worker_limit": 0})
    assert resp.status_code == 422
    err = error_of(resp)
    assert err["related_ids"]["key"] == "runtime.worker_limit"

    resp = client.patch("/api/v1/settings", json={"video.provider": "wan"})
    assert resp.status_code == 422
    assert "comfy_preset" in error_of(resp)["detail"]

    resp = client.patch("/api/v1/settings", json={"nope.nope": 1})
    assert resp.status_code == 422
    assert error_of(resp)["related_ids"]["unknown"] == ["nope.nope"]


def test_probe_failures_name_the_target_and_the_way_out(client: TestClient) -> None:
    resp = client.post("/api/v1/settings/probe", json={"what": "llm"})
    assert resp.status_code == 503, resp.text
    err = error_of(resp)
    assert err["code"] == "LLM_UNAVAILABLE"
    assert any("手动" in s for s in err["suggestions"]), "LLM 不可用必须写明手动路径"

    client.patch("/api/v1/settings", json={"video.provider": "http_api"})
    resp = client.post("/api/v1/settings/probe", json={"what": "video"})
    assert resp.status_code == 400, resp.text
    err = error_of(resp)
    assert err["code"] == "MISSING_CAPABILITY"
    assert any("设置页" in s for s in err["suggestions"])

    resp = client.post("/api/v1/settings/probe", json={"what": "空气"})
    assert resp.status_code == 422
    assert error_of(resp)["title"]


def test_reference_labels_are_configurable_but_the_count_is_not(client: TestClient) -> None:
    """「只喂一张首帧就丢人物形象」的两个旋钮：标签那一个在设置页，张数那一个刻意不在。"""
    fields = fields_of(client)
    assert "video.ref_limit" not in fields, "能收几张是模型端那份图的事实，不是我们配的数字"
    assert fields["video.ref_labels"]["kind"] == "bool"
    assert fields["video.ref_labels"]["value"] is True

    resp = client.patch("/api/v1/settings", json={"video.ref_labels": False})
    assert resp.status_code == 200, resp.text
    assert settings.video_ref_labels is False

    resp = client.patch("/api/v1/settings", json={"video.ref_limit": 3})
    assert resp.status_code == 422
    assert error_of(resp)["related_ids"]["unknown"] == ["video.ref_limit"]

    from app.services.context import ref_capacity

    cap = ref_capacity()
    assert cap.limit is None, "还没选预设时不限张数——绝不用一个猜的数字去丢用户的角色图"
    assert cap.detail, "为什么是这个上限得说出来，界面上要显示"


def test_legacy_provider_is_offered_but_marked(client: TestClient) -> None:
    rows = {p["name"]: p for p in client.get("/api/v1/settings").json()["providers"]}
    assert rows["comfy_preset"]["legacy"] is False
    assert rows["comfy_workflow"]["legacy"] is True, "旧绑定路径要标成兼容选项"


def test_system_prompt_is_configurable_but_the_shape_is_not(client: TestClient) -> None:
    """「AI 拆出来的场景不够好」得能自己改那段话——但改不到 JSON 形状那一段。"""
    from app.ai import prompts

    field = fields_of(client)["prompt.breakdown"]
    assert field["kind"] == "text"
    assert field["source"] == "default"
    assert field["builtin"] == prompts.BREAKDOWN_TASK, "内置文案只有后端一份，设置页照它画占位"
    assert field["impact"], "配错了会怎样得由后端给文案，前端不重写一遍"

    resp = client.patch("/api/v1/settings", json={"prompt.breakdown": "只拆成两幕，每幕一个镜头。"})
    assert resp.status_code == 200, resp.text
    assert fields_of(client)["prompt.breakdown"]["source"] == "file"

    text = prompts.breakdown()
    assert text.startswith("只拆成两幕"), "填了就是替换那一段"
    assert prompts.BREAKDOWN_TASK not in text, "不是拼在内置文案后面，是换掉它"
    assert prompts.BREAKDOWN_AUDIO_POLICY in text, "声音边界不能被自定义 Prompt 绕过"
    assert prompts.BREAKDOWN_SHAPE in text, "JSON 形状契约永远由代码追加，用户改不掉"

    # 清空（哪怕只敲了空格）= 恢复内置默认，而不是存一段空提示词
    resp = client.patch("/api/v1/settings", json={"prompt.breakdown": "   "})
    assert resp.status_code == 200, resp.text
    assert fields_of(client)["prompt.breakdown"]["source"] == "default"
    assert prompts.BREAKDOWN_TASK in prompts.breakdown()

    positive, negative = prompts.with_shot_audio_policy(
        "林昭在雨夜推门。声音设计：林昭说：“有人吗？”；雨声与木门吱呀声",
        "low quality",
    )
    assert "林昭说：“有人吗？”" in positive, "对白原文应保留在 Shot Prompt 中"
    assert "无背景音乐" in positive
    assert all(term in negative for term in prompts.SHOT_AUDIO_NEGATIVE_TERMS)
    assert prompts.with_shot_audio_policy(positive, negative) == (positive, negative), "兜底应幂等"


def test_director_prompt_reaches_both_paths(client: TestClient) -> None:
    """支持工具的端与退化路径读的是同一段可配文案；工具清单与形状仍由代码生成。"""
    from app.ai import prompts
    from app.ai.director import agent

    resp = client.patch("/api/v1/settings", json={"prompt.director": "你是一位克制的导演。"})
    assert resp.status_code == 200, resp.text
    assert prompts.director() == "你是一位克制的导演。"

    fallback = agent._fallback_system()  # noqa: SLF001 —— 就是要盯这条退化路径的拼法
    assert fallback.startswith("你是一位克制的导演。")
    assert "add_scene" in fallback, "工具清单由代码生成，不靠提示词里手抄一份"
    assert "只输出 JSON" in fallback, "形状那一段永远拼在最后"

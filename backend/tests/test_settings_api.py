"""Step 1 验收：应用级设置与配置页契约。

四件事必须成立：
  1. settings.json 的覆盖压过环境变量，且每个字段说清值是哪来的（file / env / default）；
  2. API Key 永不回明文——只回 masked 与 has_value；
  3. 取值不合法当场报四要素错误，绝不悄悄存一个坏值；
  4. 探测失败也是四要素错误，而不是一个红叉。
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


def test_legacy_provider_is_offered_but_marked(client: TestClient) -> None:
    rows = {p["name"]: p for p in client.get("/api/v1/settings").json()["providers"]}
    assert rows["comfy_preset"]["legacy"] is False
    assert rows["comfy_workflow"]["legacy"] is True, "旧绑定路径要标成兼容选项"

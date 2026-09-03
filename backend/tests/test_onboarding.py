"""新手引导与演示工程（Step 10）。

盯三件事：状态文件的读写、账单一个字节都不写、播种出来的结构是不是引导里说的那样。
不连 ComfyUI（先按住队列），也不出网——演示工程里刻意没有任何生成版本。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import error_of

API = "/api/v1"


@pytest.fixture(autouse=True)
def demo_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """把「家目录」指到 tmp_path 里。

    演示工程的默认落点是 `~/Documents/AI Video Studio/演示项目`（`_default_demo_dir`），
    所以「那个目录里是不是已经有演示工程」照真机去判：开发者自己在应用里点过一次
    「创建演示工程」之后，`demo_exists is False` 这条断言就永远失败了——而它测的本来是
    状态接口的形状，不是这台机器上恰好有什么。理由与 `no_ffmpeg` 那条完全相同。
    """
    monkeypatch.setattr(Path, "home", lambda *_: tmp_path / "home")


def _state(client: TestClient) -> dict[str, Any]:
    resp = client.get(f"{API}/onboarding")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _create_demo(client: TestClient, target: Path) -> dict[str, Any]:
    resp = client.post(f"{API}/onboarding/demo", json={"dir": str(target)})
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_first_run_and_step_roundtrip(client: TestClient) -> None:
    first = _state(client)
    assert first["first_run"] is True
    assert first["completed"] is False and first["skipped"] is False
    assert first["step"] == "welcome"
    assert first["steps"][0] == "welcome" and "tour" in first["steps"]
    assert first["default_demo_dir"]
    assert first["demo_exists"] is False

    resp = client.patch(f"{API}/onboarding", json={"step": "service"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["step"] == "service"

    again = _state(client)
    assert again["step"] == "service"
    # 文件已经落下来了，所以不再是「首次运行」——自动弹窗那条判断认的就是它。
    assert again["first_run"] is False

    done = client.patch(f"{API}/onboarding", json={"completed": True})
    assert done.status_code == 200
    assert done.json()["completed"] is True and done.json()["step"] == "service"


def test_unknown_step_is_rejected(client: TestClient) -> None:
    resp = client.patch(f"{API}/onboarding", json={"step": "nope"})
    assert resp.status_code == 422
    err = error_of(resp)
    assert err["code"] == "VALIDATION_ERROR"


def test_plan_writes_nothing(client: TestClient, tmp_path: Path) -> None:
    target = tmp_path / "demo-plan"
    resp = client.post(f"{API}/onboarding/demo/plan", json={"dir": str(target)})
    assert resp.status_code == 200, resp.text
    plan = resp.json()
    assert plan["action"] == "create" and plan["exists"] is False
    assert plan["dir"].endswith("demo-plan")
    assert plan["estimated_bytes"] > 0
    kinds = {item["kind"]: item["count"] for item in plan["items"]}
    assert kinds["scene"] >= 3 and kinds["shot"] >= kinds["scene"]
    assert kinds["scene_link"] == 3 and kinds["shot_link"] == 1
    # 账单里必须说清「没有已生成的版本」这件事，否则用户会以为演示工程能直接播。
    assert any("没有任何已生成" in w for w in plan["warnings"])
    # **一个字节都不写。**
    assert not target.exists()


def test_create_demo_is_idempotent_and_seeds_structure(client: TestClient, tmp_path: Path) -> None:
    client.post(f"{API}/queue/pause")
    target = tmp_path / "demo"
    first = _create_demo(client, target)
    assert first["created"] is True
    pid = first["project"]["id"]
    assert (target / "project.aivs.json").exists()

    summary = first["summary"]
    assert summary["characters"] == 3
    assert summary["locations"] == 2
    assert summary["props"] == 2
    assert summary["scenes"] == 4
    assert summary["shots"] == 9
    assert summary["links"] == 3

    scenes = client.get(f"{API}/projects/{pid}/scenes").json()
    assert len(scenes) == 4
    # 每一幕都有 prompt（缺了流程图会把它写进 issues）与挂上的小节点。
    for scene in scenes:
        assert scene["prompt"]
        assert scene["locations"]
    assert any(scene["cast"] for scene in scenes)

    links = client.get(f"{API}/projects/{pid}/links").json()
    assert {link["mode"] for link in links} == {"cut", "transition", "tail_frame"}

    # 演示工程里**没有任何生成版本**：这条断言就是「不造假数据」那条约束的守卫。
    cards = [
        card
        for lane in client.get(f"{API}/projects/{pid}/storyboard").json()
        for card in lane["shots"]
    ]
    assert cards and all(card["version_count"] == 0 for card in cards)

    # 第二次点：只打开，不重建，也不把幕数翻倍。
    second = _create_demo(client, target)
    assert second["created"] is False
    assert second["project"]["id"] == pid
    assert second["summary"]["scenes"] == 4
    assert len(client.get(f"{API}/projects/{pid}/scenes").json()) == 4

    state = _state(client)
    assert state["demo_dir"].endswith("demo")
    assert state["demo_exists"] is True


def test_seeded_shot_link_is_pending(client: TestClient, tmp_path: Path) -> None:
    client.post(f"{API}/queue/pause")
    pid = _create_demo(client, tmp_path / "demo")["project"]["id"]
    lanes = client.get(f"{API}/projects/{pid}/storyboard").json()
    connectors = [
        c
        for lane in lanes
        for c in [*lane["links"], *([lane["next_link"]] if lane["next_link"] else [])]
        if c["mode"] == "transition"
    ]
    assert connectors, lanes
    # 转场故意没生成——分镜板上就该显示「转场暂未生成」。
    assert all(c["pending"] is True for c in connectors)


def test_every_placeholder_has_a_description(client: TestClient, tmp_path: Path) -> None:
    client.post(f"{API}/queue/pause")
    pid = _create_demo(client, tmp_path / "demo")["project"]["id"]
    resp = client.get(f"{API}/projects/{pid}/assets/undescribed")
    assert resp.status_code == 200, resp.text
    # 演示工程本身就是「素材都写了描述」的样板：没有描述的图在 prompt 里只剩一个文件名。
    assert resp.json() == [] or resp.json().get("items") == []


def test_foreign_db_is_not_overwritten(client: TestClient, tmp_path: Path) -> None:
    target = tmp_path / "occupied"
    target.mkdir()
    (target / "project.db").write_bytes(b"SQLite format 3\x00 not ours")
    resp = client.post(f"{API}/onboarding/demo", json={"dir": str(target)})
    assert resp.status_code != 201
    err = error_of(resp)
    assert err["code"] == "CONFLICT"

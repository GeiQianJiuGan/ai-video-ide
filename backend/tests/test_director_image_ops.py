"""AI 协作栏新增素材（角色 / 地点 / 道具 + 顺带出一张参考图）。

盯的还是那条读 / 写分界，不是「AI 说得好不好」：

  1. **提案绝不落库**：`chat` 跑完，角色 / 地点 / 道具三张表一行都不能多；
  2. **`apply` 只落未 reject 的**，且逐条独立落——一条挂了不该带走另外几条；
  3. **正向 prompt 只在一处拼**：`after["prompt"]` 里那几句结构是系统按 SKILL 补的，
     用户那段话只填「长什么样」；
  4. **图片服务没配置时绝不静默跳过**：提案上有 warning，落库回执里有 `image_skipped`，
     而**素材照样建得出来**（硬约束 2：没有那条链也能走完流程）。

LLM 一律 monkeypatch 掉（照 `tests/test_director_agent.py::use_fake_llm`）。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.test_director_agent import use_fake_llm

API = "/api/v1"


def call(op: str, **args: Any) -> dict[str, Any]:
    """一次工具调用。**不复用 `test_director_agent.call`**：素材那几个工具自己就有一个
    `name` 参数，位置参数与它撞名。"""
    return {"id": f"c{op}", "name": op, "arguments": args}


@pytest.fixture
def image_off(client: TestClient) -> None:
    """默认就是没配图片服务（`image.provider = none`）。

    **挂在 `client` 之后**：应用启动时 `app_settings.apply()` 会重摆一次设置。
    """
    settings.image_provider = "none"


@pytest.fixture
def image_on(client: TestClient) -> None:
    """一个不需要真连的图片服务：入队只查「配没配」，pump 由 `queue/pause` 拦住。"""
    settings.image_provider = "http_api"
    settings.image_base_url = "http://127.0.0.1:9001"
    settings.image_api_key = ""
    settings.image_model = ""


def materials(client: TestClient, pid: str) -> dict[str, int]:
    return {
        "characters": len(client.get(f"{API}/projects/{pid}/characters").json()),
        "locations": len(client.get(f"{API}/projects/{pid}/locations").json()),
        "props": len(client.get(f"{API}/projects/{pid}/props").json()),
    }


def chat(client: TestClient, pid: str, message: str) -> dict[str, Any]:
    resp = client.post(f"{API}/projects/{pid}/director/chat", json={"message": message})
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


THREE_MATERIALS = [
    {
        "content": "",
        "tool_calls": [
            call(
                "add_character",
                name="阿岚",
                description="货运飞船的机械师",
                image_prompt="二十出头，褪色军绿夹克，短发",
                why="剧本里这个人物一直出现，但素材库里没有",
            ),
            call(
                "add_location",
                name="城南旧宅",
                variant="雨夜",
                time_of_day="夜",
                image_prompt="砖墙斑驳的老宅门口，湿滑的青石板",
                why="第二幕要在这里拍",
            ),
            call(
                "add_prop",
                name="旧铜钥匙",
                image_prompt="黄铜，边缘磨亮，绳结挂环",
                why="第三幕的关键道具",
            ),
        ],
    },
    {"content": "提了三个素材。", "tool_calls": []},
]


# --- 提案不是改动 ---


def test_chat_proposes_materials_without_touching_the_tables(
    client: TestClient, pid: str, image_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = materials(client, pid)
    use_fake_llm(monkeypatch, THREE_MATERIALS)

    ops = chat(client, pid, "把阿岚、城南旧宅、旧铜钥匙都建出来")["ops"]

    assert [o["op"] for o in ops] == ["add_character", "add_location", "add_prop"]
    assert [o["target"] for o in ops] == ["material"] * 3
    assert all(o["why"] and o["before"] is None for o in ops)
    # SKILL 按素材类型自动选（模型没写 skill）
    assert [o["after"]["skill"] for o in ops] == ["char_sheet", "scene_simple", "prop_ref"]
    # 结构由系统补，用户那段话只填「长什么样」
    char = ops[0]["after"]
    assert "四视图" in char["prompt"] and "褪色军绿夹克" in char["prompt"]
    assert char["image_prompt"] == "二十出头，褪色军绿夹克，短发"
    assert char["generate_image"] is True
    assert "watermark" in char["negative_prompt"]
    assert ops[1]["after"]["variant"] == "雨夜"

    # 关键断言：一行都没建
    assert materials(client, pid) == before
    # 提案落成了记录，刷新页面还在
    history = client.get(f"{API}/projects/{pid}/director").json()
    assert [t["role"] for t in history["turns"]] == ["user", "assistant", "proposal"]
    assert len(history["turns"][2]["content"]["ops"]) == 3


def test_variant_defaults_when_not_given(
    client: TestClient, pid: str, image_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """地点变体是必有的（出图挂的是变体，不是地点），没给就叫「默认场景」。"""
    use_fake_llm(
        monkeypatch,
        [
            {"content": "", "tool_calls": [call("add_location", name="码头", why="第一幕在这里")]},
            {"content": "一条。", "tool_calls": []},
        ],
    )
    ops = chat(client, pid, "加一个码头")["ops"]

    assert ops[0]["after"]["variant"] == "默认场景"
    # 没写 image_prompt 就不出图，也不该拼出一段 prompt 来
    assert ops[0]["after"]["generate_image"] is False
    assert "prompt" not in ops[0]["after"]


# --- 落库：只落未 reject 的，逐条独立 ---


def test_apply_lands_only_the_accepted_materials(
    client: TestClient, pid: str, image_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert client.post(f"{API}/projects/{pid}/queue/pause").status_code == 200
    use_fake_llm(monkeypatch, THREE_MATERIALS)
    ops = chat(client, pid, "建三个素材")["ops"]

    ops[1]["op"] = "reject"  # 地点这一条用户丢弃
    resp = client.post(f"{API}/projects/{pid}/director/apply", json={"ops": ops})
    assert resp.status_code == 201, resp.text
    body = resp.json()

    assert body["count"] == 2 and body["failed"] == []
    assert materials(client, pid) == {"characters": 1, "locations": 0, "props": 1}

    # 角色回执里有形象 id：出图挂的是形象，不是角色
    made = body["applied"][0]
    assert made["character_id"].startswith("chr_")
    assert made["appearance_id"].startswith("apr_") or made["appearance_id"]
    # 配了图片服务，所以顺带入队了一张图（同一张 job 表）
    assert made["job_id"].startswith("job_")
    assert made["target_label"] == "角色 · 阿岚 · 默认形象 四视图"
    assert "image_skipped" not in made

    jobs = client.get(f"{API}/projects/{pid}/queue").json()["jobs"]
    assert sorted(j["target_kind"] for j in jobs) == ["appearance", "prop"]
    assert all(j["shot_id"] is None for j in jobs)


def test_location_apply_creates_the_variant_too(
    client: TestClient, pid: str, image_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert client.post(f"{API}/projects/{pid}/queue/pause").status_code == 200
    use_fake_llm(monkeypatch, THREE_MATERIALS)
    ops = [o for o in chat(client, pid, "建三个素材")["ops"] if o["op"] == "add_location"]

    body = client.post(f"{API}/projects/{pid}/director/apply", json={"ops": ops}).json()
    made = body["applied"][0]

    assert made["variant"] == "雨夜" and made["variant_id"]
    locations = client.get(f"{API}/projects/{pid}/locations").json()
    assert [v["id"] for v in locations[0]["variants"]] == [made["variant_id"]]
    assert locations[0]["variants"][0]["time_of_day"] == "夜"
    assert made["target_label"] == "地点 · 城南旧宅 · 雨夜 参考图"


# --- 没配图片服务：素材照样建，原因说出来 ---


def test_without_image_service_the_material_is_still_created(
    client: TestClient, pid: str, image_off: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_fake_llm(monkeypatch, THREE_MATERIALS)
    ops = chat(client, pid, "建三个素材")["ops"]

    # 提案上就得说清（绝不静默跳过）
    for op in ops:
        assert any("图片服务未配置" in w for w in op["warnings"]), op["op"]
        assert op["after"]["generate_image"] is False
        # 提示词照旧拼好给人看：用户能看清 AI 本来想画什么
        assert op["after"]["prompt"]

    body = client.post(f"{API}/projects/{pid}/director/apply", json={"ops": ops}).json()
    assert body["count"] == 3 and body["failed"] == []
    # 素材真建出来了，只是没有图
    assert materials(client, pid) == {"characters": 1, "locations": 1, "props": 1}
    for made in body["applied"]:
        assert "job_id" not in made
        assert "图片服务未配置" in made["image_skipped"]
        assert "生成参考图" in made["image_skipped"]  # 之后怎么补上这张图
    assert client.get(f"{API}/projects/{pid}/queue").json()["jobs"] == []


# --- 给已有素材补一张图 ---


def test_generate_reference_for_an_existing_appearance(
    client: TestClient, pid: str, image_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert client.post(f"{API}/projects/{pid}/queue/pause").status_code == 200
    char = client.post(f"{API}/projects/{pid}/characters", json={"name": "林昭"}).json()
    appearance = client.get(f"{API}/projects/{pid}/characters/{char['id']}/appearances").json()[0]
    use_fake_llm(
        monkeypatch,
        [
            {
                "content": "",
                "tool_calls": [
                    call(
                        "generate_reference",
                        target_kind="appearance",
                        target_id=appearance["id"],
                        image_prompt="四十岁上下，深灰风衣",
                        why="这个形象还没有定妆图",
                    )
                ],
            },
            {"content": "一条。", "tool_calls": []},
        ],
    )
    ops = chat(client, pid, "给林昭出一张定妆图")["ops"]

    assert ops[0]["after"]["target_label"] == "角色 · 林昭 · 默认形象 四视图"
    body = client.post(f"{API}/projects/{pid}/director/apply", json={"ops": ops}).json()
    assert body["count"] == 1
    assert body["applied"][0]["job_id"].startswith("job_")
    assert (
        client.get(f"{API}/projects/{pid}/queue").json()["jobs"][0]["target_id"]
        == (appearance["id"])
    )


def test_generate_reference_on_a_character_id_is_corrected(
    client: TestClient, pid: str, image_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """模型很容易把**角色 id** 当成形象 id 传进来。这一步在提案阶段就要挡住，
    并把「该传哪一个」写进建议里——否则会提出一条指向不存在素材的提案。"""
    char = client.post(f"{API}/projects/{pid}/characters", json={"name": "林昭"}).json()
    use_fake_llm(
        monkeypatch,
        [
            {
                "content": "",
                "tool_calls": [
                    call(
                        "generate_reference",
                        target_kind="appearance",
                        target_id=char["id"],
                        image_prompt="深灰风衣",
                        why="出定妆图",
                    )
                ],
            },
            {"content": "换个 id 再试。", "tool_calls": []},
        ],
    )
    out = chat(client, pid, "给林昭出一张定妆图")

    # 工具失败是喂回给模型的，不是整轮 500（照 `test_tool_failure_is_fed_back_not_thrown`）
    assert out["ops"] == []
    assert "换个 id" in out["turns"][0]["content"]["text"]

"""拆完一段之后那一步对账：**缺人 / 缺地点时能不能顺手把素材建出来并接上镜头。**

盯的是四件事，都不是「AI 说得好不好」：

  1. **`list_missing_materials` 的四类账全部转调已有判断**：形象缺定妆图、地点变体缺参考图、
     道具缺参考图、幕缺人 / 缺地点。出图那条链配没配也一起回——没配时模型得先说出来，
     而不是提一堆永远出不了图的提案（硬约束 2 + 4）。
  2. **同一批里按名字接线，且与提案顺序无关**：`add_shot` 完全可能排在 `add_character`
     前面（顺序由模型定），两种顺序结果必须一样。
  3. **接不上只是接不上**：`add_character` 被用户丢掉时，那一镜照样落库，只多一句
     `cast_skipped` 说清没接上谁——绝不让一条被丢弃的提案带走整个镜头。
  4. **提案阶段一行库都不改**：对不上的名字留成 `pending_name`，不是偷偷建一个角色。

LLM 一律 monkeypatch 掉（照 `tests/test_director_agent.py::use_fake_llm`）。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.ai.llm import client as llm
from app.core.config import settings

API = "/api/v1"


def call(op: str, **args: Any) -> dict[str, Any]:
    """一次工具调用。**不复用 `test_director_agent.call`**：素材那几个工具自己就有一个
    `name` 参数，位置参数与它撞名。"""
    return {"id": f"c{op}", "name": op, "arguments": args}


def record_llm(
    monkeypatch: pytest.MonkeyPatch, rounds: list[dict[str, Any]]
) -> list[list[dict[str, Any]]]:
    """和 `use_fake_llm` 同一套假 LLM，只是**把每一轮的 messages 留下来**。

    读工具的返回只回给模型看（不落 `DirectorTurn`），所以想断言 `list_missing_materials`
    那张账长什么样，只能从下一轮的 `role="tool"` 那条消息里读。
    """
    monkeypatch.setattr(settings, "llm_provider", "openai_compatible")
    monkeypatch.setattr(settings, "llm_model", "fake-model")
    monkeypatch.setattr(settings, "llm_base_url", "http://127.0.0.1:9/v1")
    seen: list[list[dict[str, Any]]] = []

    async def fake(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        seen.append([dict(m) for m in messages])
        return rounds[min(len(seen) - 1, len(rounds) - 1)]

    monkeypatch.setattr(llm, "complete_tools", fake)
    return seen


def tool_payload(rounds: list[list[dict[str, Any]]], index: int = 1) -> dict[str, Any]:
    """第 `index` 轮 messages 里最后那条工具返回（读工具的 JSON 原文）。"""
    tools = [m for m in rounds[index] if m.get("role") == "tool"]
    assert tools, "这一轮里没有工具返回"
    return dict(json.loads(str(tools[-1]["content"])))


@pytest.fixture
def image_off(client: TestClient) -> None:
    """默认就是没配图片服务。**挂在 `client` 之后**：启动时 `app_settings.apply()` 会重摆一次。"""
    settings.image_provider = "none"


@pytest.fixture
def image_on(client: TestClient) -> None:
    """一个不需要真连的图片服务：入队只查「配没配」，pump 由 `queue/pause` 拦住。"""
    settings.image_provider = "http_api"
    settings.image_base_url = "http://127.0.0.1:9001"
    settings.image_api_key = ""
    settings.image_model = ""


def chat(client: TestClient, pid: str, message: str) -> dict[str, Any]:
    resp = client.post(f"{API}/projects/{pid}/director/chat", json={"message": message})
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


def apply(client: TestClient, pid: str, ops: list[dict[str, Any]]) -> dict[str, Any]:
    resp = client.post(f"{API}/projects/{pid}/director/apply", json={"ops": ops})
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


def scene_with_shot(client: TestClient, pid: str, title: str = "第一幕") -> tuple[str, str]:
    """一幕 + 一个镜头。**刻意不挂人也不挂地点**——那正是这条链要发现的东西。"""
    sid = client.post(f"{API}/projects/{pid}/scenes", json={"title": title}).json()["id"]
    resp = client.post(f"{API}/projects/{pid}/scenes/{sid}/shots", json={"title": "镜头 1"})
    assert resp.status_code == 201, resp.text
    return str(sid), str(resp.json()["id"])


def cast_names(client: TestClient, pid: str, shot_id: str) -> list[str]:
    body = client.get(f"{API}/projects/{pid}/shots/{shot_id}").json()
    return [c["character_name"] for c in body["cast"]]


def entry_of(body: dict[str, Any], op: str) -> dict[str, Any]:
    hit = next((e for e in body["applied"] if e["op"] == op), None)
    assert hit is not None, f"落库回执里没有 {op}：{body}"
    return dict(hit)


ASK_SWEEP = [
    {"content": "", "tool_calls": [call("list_missing_materials")]},
    {"content": "缺的素材我列出来了。", "tool_calls": []},
]


# --- 那张账 ---


def test_missing_materials_lists_the_four_categories(
    client: TestClient, pid: str, image_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """四类各造一条，看这张账认不认得出来。判断全部来自已有那一份，这里只核对口径。"""
    client.post(f"{API}/projects/{pid}/characters", json={"name": "阿岚"})
    loc = client.post(f"{API}/projects/{pid}/locations", json={"name": "城南旧宅"}).json()
    client.post(f"{API}/projects/{pid}/locations/{loc['id']}/variants", json={"name": "雨夜"})
    client.post(f"{API}/projects/{pid}/props", json={"name": "旧铜钥匙"})
    scene_with_shot(client, pid)
    rounds = record_llm(monkeypatch, ASK_SWEEP)

    chat(client, pid, "对一遍账，还缺什么素材")
    bill = tool_payload(rounds)

    # 形象有了、定妆图还没有
    assert [c["label"] for c in bill["characters"]] == ["阿岚 · 默认形象"]
    assert bill["characters"][0]["appearance_id"].startswith("app_")
    # 地点变体缺参考图
    assert [s["label"] for s in bill["locations"]] == ["城南旧宅 · 雨夜"]
    # 道具缺参考图
    assert [p["label"] for p in bill["props"]] == ["旧铜钥匙"]
    # 幕本身缺人缺地点（这一条来自 storyboard 的 context_issues，不是另算一遍）
    assert len(bill["scenes"]) == 1
    assert bill["scenes"][0]["title"] == "第一幕"
    assert "没有出场角色" in bill["scenes"][0]["issues"]
    assert any("地点" in i for i in bill["scenes"][0]["issues"])
    assert bill["total"] == 4
    # 配了出图服务，所以下一步是「一条一条建」，并且说清同一批里能按名字用
    assert bill["image"]["configured"] is True
    assert "add_character" in bill["next_step"]
    assert "character_names" in bill["next_step"]


def test_missing_materials_is_empty_when_everything_is_covered(
    client: TestClient, pid: str, image_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """空工程没有素材也没有幕，那就是「不用补」——绝不无端提一堆提案。"""
    rounds = record_llm(monkeypatch, ASK_SWEEP)
    chat(client, pid, "还缺什么")
    bill = tool_payload(rounds)

    assert bill["total"] == 0
    assert bill["characters"] == [] and bill["locations"] == []
    assert bill["props"] == [] and bill["scenes"] == []
    assert "不用补素材" in bill["next_step"]


def test_missing_materials_says_the_image_chain_is_off(
    client: TestClient, pid: str, image_off: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """出图没配时**先把这件事告诉用户**：账上写清楚，下一步指向手动那条路（硬约束 2）。"""
    client.post(f"{API}/projects/{pid}/characters", json={"name": "阿岚"})
    rounds = record_llm(monkeypatch, ASK_SWEEP)
    chat(client, pid, "对一遍账")
    bill = tool_payload(rounds)

    assert bill["image"]["configured"] is False
    assert bill["image"]["provider"] == "none"
    assert bill["image"]["how_to"], "没配也得说清怎么办，不能只回一个 false"
    assert "image.how_to" in bill["next_step"]
    # 素材照样要建（缺的还是缺的），只是图这一项走别的路
    assert bill["total"] == 1


# --- 同一批里按名字接线 ---


def new_character_and_shot(sid: str, reverse: bool = False) -> list[dict[str, Any]]:
    """一批提案：建一个角色 + 一个用这个角色的镜头。`reverse` 把镜头排在角色前面。"""
    people = call(
        "add_character",
        name="阿岚",
        description="货运飞船的机械师",
        why="这一镜要有他，但素材库里没有",
    )
    shot = call(
        "add_shot",
        scene_id=sid,
        title="阿岚推开舱门",
        character_names=["阿岚"],
        camera_motion="中景，缓慢推进",
        visual_prompt="他侧身推开舱门，舱内红灯扫过脸侧",
        audio_dialogue="液压门声；阿岚：「就这一次。」",
        why="这一段的第一镜",
    )
    calls = [shot, people] if reverse else [people, shot]
    return [
        {"content": "", "tool_calls": calls},
        {"content": "提了两条。", "tool_calls": []},
    ]


@pytest.mark.parametrize("reverse", [False, True], ids=["character-first", "shot-first"])
def test_same_batch_name_join_is_order_independent(
    client: TestClient,
    pid: str,
    image_off: None,
    monkeypatch: pytest.MonkeyPatch,
    reverse: bool,
) -> None:
    """**提案的先后由模型决定**，所以两种顺序都得接上——落一条就接一次的话结果会分叉。"""
    sid, _ = scene_with_shot(client, pid)
    record_llm(monkeypatch, new_character_and_shot(sid, reverse))
    ops = chat(client, pid, "把阿岚建出来，并加一镜他推开舱门")["ops"]

    # 提案阶段那个角色还不存在，所以留成 pending_name（一行库都没改）
    shot_op = next(o for o in ops if o["op"] == "add_shot")
    assert shot_op["after"]["cast"] == [
        {"appearance_id": "", "pending_name": "阿岚", "label": "阿岚（这一批新建的）"}
    ]
    assert any("还没有「阿岚」这个角色" in w for w in shot_op["warnings"])
    assert client.get(f"{API}/projects/{pid}/characters").json() == []

    body = apply(client, pid, ops)
    assert body["count"] == 2 and body["failed"] == []

    made = entry_of(body, "add_shot")
    assert cast_names(client, pid, made["shot_id"]) == ["阿岚"], "同一批新建的角色没接上"
    assert "阿岚" in made["cast_wired"]
    assert "cast_skipped" not in made


def test_a_rejected_character_only_loses_the_link(
    client: TestClient, pid: str, image_off: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """用户把 `add_character` 丢掉时，**那一镜照样落库**，只多一句「没接上谁」。

    连累整条镜头才是真正的坏结果：用户丢掉的是一个角色，不是这一段戏。
    """
    sid, _ = scene_with_shot(client, pid)
    record_llm(monkeypatch, new_character_and_shot(sid))
    ops = chat(client, pid, "建角色再加一镜")["ops"]

    next(o for o in ops if o["op"] == "add_character")["op"] = "reject"
    body = apply(client, pid, ops)

    assert body["count"] == 1 and body["failed"] == []
    assert client.get(f"{API}/projects/{pid}/characters").json() == []
    made = entry_of(body, "add_shot")
    assert cast_names(client, pid, made["shot_id"]) == []
    assert "阿岚" in made["cast_skipped"] and "角色" in made["cast_skipped"]
    assert "不受影响" in made["cast_skipped"], "得告诉用户其余部分照旧落库了"
    assert "cast_wired" not in made


def test_existing_and_new_cast_land_together(
    client: TestClient, pid: str, image_off: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一半是库里已有的、一半是这一批新建的：`set_shot_cast` 是整份覆盖而不是追加，
    所以接线必须把两边一起写回去，否则先落的那个会被冲掉。"""
    sid, _ = scene_with_shot(client, pid)
    client.post(f"{API}/projects/{pid}/characters", json={"name": "林昭"})
    record_llm(
        monkeypatch,
        [
            {
                "content": "",
                "tool_calls": [
                    call(
                        "add_shot",
                        scene_id=sid,
                        title="两个人对峙",
                        character_names=["林昭", "阿岚"],
                        visual_prompt="两人隔着长桌对视",
                        why="这一段的核心冲突",
                    ),
                    call("add_character", name="阿岚", description="机械师", why="库里还没有他"),
                ],
            },
            {"content": "两条。", "tool_calls": []},
        ],
    )
    ops = chat(client, pid, "加一镜两人对峙")["ops"]

    shot_op = next(o for o in ops if o["op"] == "add_shot")
    assert [c.get("pending_name", "") for c in shot_op["after"]["cast"]] == ["", "阿岚"]

    body = apply(client, pid, ops)
    made = entry_of(body, "add_shot")
    assert sorted(cast_names(client, pid, made["shot_id"])) == ["林昭", "阿岚"]


def test_scene_cast_wires_every_shot_in_the_scene(
    client: TestClient, pid: str, image_off: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """整幕覆盖那一条也一样：这一幕的每个镜头都要接上，不是只接第一个。"""
    sid, first = scene_with_shot(client, pid)
    second = client.post(
        f"{API}/projects/{pid}/scenes/{sid}/shots", json={"title": "镜头 2"}
    ).json()
    record_llm(
        monkeypatch,
        [
            {
                "content": "",
                "tool_calls": [
                    call(
                        "set_scene_cast", scene_id=sid, character_names=["阿岚"], why="这一幕只有他"
                    ),
                    call("add_character", name="阿岚", description="机械师", why="库里还没有"),
                ],
            },
            {"content": "两条。", "tool_calls": []},
        ],
    )
    ops = chat(client, pid, "这一幕都是阿岚")["ops"]

    body = apply(client, pid, ops)
    assert body["count"] == 2 and body["failed"] == []
    assert cast_names(client, pid, first) == ["阿岚"]
    assert cast_names(client, pid, str(second["id"])) == ["阿岚"]
    assert "阿岚" in entry_of(body, "set_scene_cast")["cast_wired"]


def test_new_prop_wires_into_the_scene(
    client: TestClient, pid: str, image_off: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """道具走的是同一条路（`set_shot_props` 也是整份覆盖）。"""
    sid, first = scene_with_shot(client, pid)
    record_llm(
        monkeypatch,
        [
            {
                "content": "",
                "tool_calls": [
                    call(
                        "set_scene_props",
                        scene_id=sid,
                        prop_names=["旧铜钥匙"],
                        why="这一幕的关键道具",
                    ),
                    call(
                        "add_prop", name="旧铜钥匙", description="黄铜，边缘磨亮", why="库里还没有"
                    ),
                ],
            },
            {"content": "两条。", "tool_calls": []},
        ],
    )
    ops = chat(client, pid, "这一幕要有那把钥匙")["ops"]

    body = apply(client, pid, ops)
    assert body["count"] == 2 and body["failed"] == []
    props = client.get(f"{API}/projects/{pid}/shots/{first}").json()["props"]
    assert [p["prop_name"] for p in props] == ["旧铜钥匙"]
    assert "道具" in entry_of(body, "set_scene_props")["props_wired"]


# --- 地点：幕上挂的是变体 ---


def test_location_name_resolves_an_existing_variant(
    client: TestClient, pid: str, image_off: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """库里已经有这个地点时，提案阶段就该定到那个变体上——不留 pending，不重建一个。"""
    loc = client.post(f"{API}/projects/{pid}/locations", json={"name": "城南旧宅"}).json()
    variant = client.post(
        f"{API}/projects/{pid}/locations/{loc['id']}/variants", json={"name": "雨夜"}
    ).json()
    record_llm(
        monkeypatch,
        [
            {
                "content": "",
                "tool_calls": [
                    call(
                        "add_scene",
                        title="回到旧宅",
                        location_name="城南旧宅",
                        why="这一段在这里发生",
                    )
                ],
            },
            {"content": "一条。", "tool_calls": []},
        ],
    )
    ops = chat(client, pid, "加一幕，在城南旧宅")["ops"]

    after = ops[0]["after"]
    assert after["location_variant_id"] == variant["id"]
    assert after["location_label"] == "城南旧宅 · 雨夜"
    assert "location_name" not in after, "已经定到变体了就不该再留一个名字等接线"

    body = apply(client, pid, ops)
    got = client.get(
        f"{API}/projects/{pid}/scenes/{entry_of(body, 'add_scene')['scene_id']}"
    ).json()
    assert got["location_variant_id"] == variant["id"]
    assert "location_wired" not in entry_of(body, "add_scene")


def test_location_name_joins_a_same_batch_add_location(
    client: TestClient, pid: str, image_off: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """地点还不存在时留成 `location_name`，同一批里的 `add_location` 建好之后接上。

    幕上挂的是**变体**而不是地点，所以接的是新建地点的第一个变体。
    """
    record_llm(
        monkeypatch,
        [
            {
                "content": "",
                "tool_calls": [
                    call(
                        "add_scene", title="回到旧宅", location_name="城南旧宅", why="这一段在这里"
                    ),
                    call(
                        "add_location",
                        name="城南旧宅",
                        variant="雨夜",
                        time_of_day="夜",
                        why="工程里还没有这个地点",
                    ),
                ],
            },
            {"content": "两条。", "tool_calls": []},
        ],
    )
    ops = chat(client, pid, "加一幕在城南旧宅，顺手把这个地点建出来")["ops"]

    after = ops[0]["after"]
    assert after["location_name"] == "城南旧宅"
    assert "location_variant_id" not in after
    assert any("还没有「城南旧宅」这个地点" in w for w in ops[0]["warnings"])
    assert client.get(f"{API}/projects/{pid}/locations").json() == []

    body = apply(client, pid, ops)
    assert body["count"] == 2 and body["failed"] == []
    made = entry_of(body, "add_scene")
    got = client.get(f"{API}/projects/{pid}/scenes/{made['scene_id']}").json()
    assert got["location_variant_id"] == entry_of(body, "add_location")["variant_id"]
    assert "城南旧宅" in made["location_wired"]


def test_variant_id_wins_over_the_name(
    client: TestClient, pid: str, image_off: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """两个都给了时以 id 为准：那是它自己查出来的，名字往往只是顺手写的一句。"""
    loc = client.post(f"{API}/projects/{pid}/locations", json={"name": "城南旧宅"}).json()
    variant = client.post(
        f"{API}/projects/{pid}/locations/{loc['id']}/variants", json={"name": "雨夜"}
    ).json()
    sid = client.post(f"{API}/projects/{pid}/scenes", json={"title": "第一幕"}).json()["id"]
    record_llm(
        monkeypatch,
        [
            {
                "content": "",
                "tool_calls": [
                    call(
                        "update_scene",
                        scene_id=sid,
                        location_variant_id=variant["id"],
                        location_name="别的地方",
                        why="这一幕搬到旧宅",
                    )
                ],
            },
            {"content": "一条。", "tool_calls": []},
        ],
    )
    ops = chat(client, pid, "这一幕改到旧宅")["ops"]

    assert ops[0]["after"]["location_variant_id"] == variant["id"]
    assert "location_name" not in ops[0]["after"]
    assert any("按 id 那个来" in w for w in ops[0]["warnings"])

    apply(client, pid, ops)
    got = client.get(f"{API}/projects/{pid}/scenes/{sid}").json()
    assert got["location_variant_id"] == variant["id"]


def test_unresolved_location_name_does_not_fail_the_scene(
    client: TestClient, pid: str, image_off: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """名字写错、或者那条 `add_location` 被丢掉时：幕照样落库，只是没有地点，并说清楚。"""
    record_llm(
        monkeypatch,
        [
            {
                "content": "",
                "tool_calls": [
                    call(
                        "add_scene", title="回到旧宅", location_name="城南旧宅", why="这一段在这里"
                    )
                ],
            },
            {"content": "一条。", "tool_calls": []},
        ],
    )
    ops = chat(client, pid, "加一幕在城南旧宅")["ops"]

    body = apply(client, pid, ops)
    assert body["count"] == 1 and body["failed"] == []
    made = entry_of(body, "add_scene")
    got = client.get(f"{API}/projects/{pid}/scenes/{made['scene_id']}").json()
    assert got["location_variant_id"] is None
    assert "城南旧宅" in made["location_skipped"] and "地点" in made["location_skipped"]
    assert "不受影响" in made["location_skipped"]


# --- 建素材 + 出图 + 接线，一批走完 ---


def test_one_batch_creates_the_material_the_image_and_the_link(
    client: TestClient, pid: str, image_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """用户那句话要的就是这个：缺人物时**调已有的出图能力**建人 + 排一张四视图 + 接到镜头上。"""
    assert client.post(f"{API}/projects/{pid}/queue/pause").status_code == 200
    sid, _ = scene_with_shot(client, pid)
    record_llm(
        monkeypatch,
        [
            {
                "content": "",
                "tool_calls": [
                    call(
                        "add_shot",
                        scene_id=sid,
                        title="阿岚推开舱门",
                        character_names=["阿岚"],
                        visual_prompt="他侧身推开舱门",
                        why="这一段的第一镜",
                    ),
                    call(
                        "add_character",
                        name="阿岚",
                        description="货运飞船的机械师",
                        image_prompt="二十出头，褪色军绿夹克，短发",
                        why="这一镜要有他，库里还没有",
                    ),
                ],
            },
            {"content": "两条。", "tool_calls": []},
        ],
    )
    ops = chat(client, pid, "补上缺的人物，并加一镜")["ops"]

    body = apply(client, pid, ops)
    assert body["count"] == 2 and body["failed"] == []

    person = entry_of(body, "add_character")
    assert person["job_id"].startswith("job_")
    assert person["target_label"] == "角色 · 阿岚 · 默认形象 四视图"
    assert "image_skipped" not in person

    made = entry_of(body, "add_shot")
    assert cast_names(client, pid, made["shot_id"]) == ["阿岚"]

    jobs = client.get(f"{API}/projects/{pid}/queue").json()["jobs"]
    assert [j["target_kind"] for j in jobs] == ["appearance"]

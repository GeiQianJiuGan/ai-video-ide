"""Step 6 验收：AI 导演 agent。

这个文件盯的是**边界**，而不是「AI 说得好不好」：

  1. **提案绝不落库**。`chat` 跑完，库里的幕数一个都不能多——数据库是用户的，
     改它必须经过逐条点头；
  2. **`apply` 只落未 reject 的**。丢弃一条，那一条就得真的没发生；
  3. **没配 LLM 不是崩溃**：`LLM_UNAVAILABLE` 四要素齐全，且建议里必须写出手动路径；
  4. **转满轮数也不白干**：提案照旧落成一条记录，刷新页面还在。

LLM 一律 monkeypatch 掉——测的是我们这一侧的行为，不是某个模型的脾气。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.ai.director import agent
from app.ai.llm import client as llm
from app.core.config import settings
from tests.conftest import error_of

API = "/api/v1"


def use_fake_llm(monkeypatch: pytest.MonkeyPatch, rounds: list[dict[str, Any]]) -> list[int]:
    """把 LLM 换成一串预先写好的回合。返回一个计数器，方便断言真的被调了几轮。"""
    monkeypatch.setattr(settings, "llm_provider", "openai_compatible")
    monkeypatch.setattr(settings, "llm_model", "fake-model")
    monkeypatch.setattr(settings, "llm_base_url", "http://127.0.0.1:9/v1")
    calls: list[int] = []

    async def fake(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        calls.append(len(messages))
        return rounds[min(len(calls) - 1, len(rounds) - 1)]

    monkeypatch.setattr(llm, "complete_tools", fake)
    return calls


def call(name: str, **args: Any) -> dict[str, Any]:
    return {"id": f"c{name}", "name": name, "arguments": args}


def scene(client: TestClient, pid: str, title: str) -> str:
    resp = client.post(f"{API}/projects/{pid}/scenes", json={"title": title})
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def test_no_llm_points_at_the_manual_path(client: TestClient, pid: str) -> None:
    """默认 provider = none。这不是错误页，是一条带手动出路的结构化错误。"""
    resp = client.post(f"{API}/projects/{pid}/director/chat", json={"message": "加一幕雨夜追车"})
    assert resp.status_code == 503
    err = error_of(resp)
    assert err["code"] == "LLM_UNAVAILABLE"
    assert any("手动" in s for s in err["suggestions"]), "没配 LLM 时必须告诉用户手动怎么走"
    # 协作栏本身仍然可读：它据此显示去配置页的引导，而不是一个红叉
    body = client.get(f"{API}/projects/{pid}/director").json()
    assert body["llm"]["configured"] is False
    assert body["turns"] == []


def test_proposal_never_touches_the_database(
    client: TestClient, pid: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = scene(client, pid, "第一幕")
    second = scene(client, pid, "第二幕")
    calls = use_fake_llm(
        monkeypatch,
        [
            {"content": "", "tool_calls": [call("list_scenes")]},
            {
                "content": "",
                "tool_calls": [
                    call("add_scene", title="雨夜追车", why="第二幕之后缺一个动作段落"),
                    call("update_scene", scene_id=first, title="第一幕 · 改名", why="更贴合"),
                    call(
                        "set_link",
                        from_scene_id=first,
                        to_scene_id=second,
                        mode="transition",
                        duration=1.5,
                        why="两幕之间需要过渡",
                    ),
                ],
            },
            {"content": "提了三条改动。", "tool_calls": []},
        ],
    )

    resp = client.post(f"{API}/projects/{pid}/director/chat", json={"message": "帮我补一幕"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert len(calls) == 3, "读工具那一轮之后应该还有提案轮与收尾轮"
    ops = body["ops"]
    assert [o["op"] for o in ops] == ["add_scene", "update_scene", "set_link"]
    assert all(o["why"] for o in ops), "每条提案都要说清为什么"
    # update / set_link 要能画出 Diff：before 得是库里现在的样子
    assert ops[1]["before"]["title"] == "第一幕"
    assert ops[2]["before"] is None and ops[2]["after"]["mode"] == "transition"

    # 关键断言：一行都没改
    scenes = client.get(f"{API}/projects/{pid}/scenes").json()
    assert [s["title"] for s in scenes] == ["第一幕", "第二幕"]
    assert client.get(f"{API}/projects/{pid}/links").json() == []

    # 提案落成了记录，刷新页面还在
    history = client.get(f"{API}/projects/{pid}/director").json()
    roles = [t["role"] for t in history["turns"]]
    assert roles == ["user", "assistant", "proposal"]
    assert len(history["turns"][2]["content"]["ops"]) == 3


def test_apply_lands_only_the_accepted_ops(
    client: TestClient, pid: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = scene(client, pid, "第一幕")
    use_fake_llm(
        monkeypatch,
        [
            {
                "content": "",
                "tool_calls": [
                    call(
                        "add_scene",
                        title="雨夜追车",
                        shots=[{"title": "雨中疾驰", "duration": 5}],
                        why="补动作段落",
                    ),
                    call("update_scene", scene_id=first, title="不该被改", why="随手改个名"),
                ],
            },
            {"content": "两条。", "tool_calls": []},
        ],
    )
    ops = client.post(f"{API}/projects/{pid}/director/chat", json={"message": "补一幕"}).json()[
        "ops"
    ]

    # 用户丢弃第二条：照 story 的老规矩，把 op 改成 reject
    ops[1]["op"] = "reject"
    resp = client.post(f"{API}/projects/{pid}/director/apply", json={"ops": ops})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["count"] == 1 and body["failed"] == []
    assert body["applied"][0]["shots_created"] == 1

    scenes = client.get(f"{API}/projects/{pid}/scenes").json()
    assert [s["title"] for s in scenes] == ["第一幕", "雨夜追车"], "被丢弃的改名不该发生"
    assert scenes[1]["shot_count"] == 1


def test_over_limit_keeps_what_it_already_proposed(
    client: TestClient, pid: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """它可以转不动，但不能让用户白等一场。"""
    use_fake_llm(
        monkeypatch,
        [{"content": "", "tool_calls": [call("add_scene", title="又一幕", why="停不下来")]}],
    )
    resp = client.post(f"{API}/projects/{pid}/director/chat", json={"message": "一直加"})
    assert resp.status_code == 400
    err = error_of(resp)
    assert any("提案" in s for s in err["suggestions"]), "必须告诉用户已产出的提案仍可审阅"

    history = client.get(f"{API}/projects/{pid}/director").json()
    proposal = next(t for t in history["turns"] if t["role"] == "proposal")
    assert len(proposal["content"]["ops"]) == agent.MAX_ROUNDS, "每轮各提一条，一条都不该丢"
    assert client.get(f"{API}/projects/{pid}/scenes").json() == []


def test_tool_failure_is_fed_back_not_thrown(
    client: TestClient, pid: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """模型编了一个不存在的 scene_id：这一步失败，整轮照旧走完。"""
    use_fake_llm(
        monkeypatch,
        [
            {"content": "", "tool_calls": [call("update_scene", scene_id="scn_不存在", title="x")]},
            {
                "content": "改用新增。",
                "tool_calls": [],
            },
        ],
    )
    resp = client.post(f"{API}/projects/{pid}/director/chat", json={"message": "改第九幕"})
    assert resp.status_code == 201, resp.text
    assert resp.json()["ops"] == []
    assert "改用新增" in resp.json()["turns"][0]["content"]["text"]


def test_degrades_to_one_shot_json_without_tool_support(
    client: TestClient, pid: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ollama 那类端不走工具循环，但提案形状必须一模一样。"""
    monkeypatch.setattr(settings, "llm_provider", "ollama")
    monkeypatch.setattr(settings, "llm_model", "qwen")
    payload = {
        "reply": "提了一条。",
        "ops": [
            {"tool": "add_scene", "args": {"title": "码头夜戏", "why": "结尾缺一个收束"}},
            {"tool": "delete_scene", "args": {"scene_id": "scn_没有这个", "why": "乱来"}},
        ],
    }

    async def fake_json(system: str, user: str) -> dict[str, Any]:
        assert "工程现状" in user, "退化路径必须把现状喂给模型——它没法自己查"
        json.loads(user.split("工程现状：", 1)[1].split("\n\n用户的要求", 1)[0])
        return payload

    monkeypatch.setattr(llm, "complete_json", fake_json)
    body = client.post(f"{API}/projects/{pid}/director/chat", json={"message": "补个结尾"}).json()
    assert body["degraded"] is True
    assert [o["op"] for o in body["ops"]] == ["add_scene"], "指向不存在的幕那条不成立"
    assert client.get(f"{API}/projects/{pid}/scenes").json() == []


# --- 剧本原文分段读（「不再超时」的那一半） ---


def test_read_script_pages_through_the_raw_text(
    client: TestClient, pid: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一段一段地读：offset / next_offset / done 必须自洽，否则模型会漏读或重复读。"""
    text = "".join(f"第{i}句。" for i in range(200))
    client.patch(f"{API}/projects/{pid}/story", json={"raw_text": text})

    seen: list[dict[str, Any]] = []

    async def fake(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        if tool_msgs:
            seen.append(json.loads(str(tool_msgs[-1]["content"])))
        if len(seen) >= 2:
            return {"content": "读到一半，等你说继续。", "tool_calls": []}
        offset = seen[-1]["next_offset"] if seen else 0
        return {
            "content": "",
            "tool_calls": [call("read_script", offset=offset, limit=300)],
        }

    monkeypatch.setattr(settings, "llm_provider", "openai_compatible")
    monkeypatch.setattr(settings, "llm_model", "fake-model")
    monkeypatch.setattr(settings, "llm_base_url", "http://127.0.0.1:9/v1")
    monkeypatch.setattr(llm, "complete_tools", fake)

    resp = client.post(
        f"{API}/projects/{pid}/director/chat", json={"message": "开始拆", "scope": "script"}
    )
    assert resp.status_code == 201, resp.text
    assert len(seen) == 2
    assert seen[0]["total"] == len(text)
    assert seen[0]["offset"] == 0 and seen[0]["next_offset"] == 300
    assert seen[0]["text"] == text[:300] and seen[0]["done"] is False
    assert seen[1]["offset"] == 300 and seen[1]["text"] == text[300:600]


def test_read_script_without_raw_text_says_what_to_do(
    client: TestClient, pid: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没存剧本原文时，回给模型的是四要素错误的文字，不是一句空字符串。"""
    use_fake_llm(
        monkeypatch,
        [
            {"content": "", "tool_calls": [call("read_script", offset=0)]},
            {"content": "没有原文可读。", "tool_calls": []},
        ],
    )
    resp = client.post(f"{API}/projects/{pid}/director/chat", json={"message": "开始拆"})
    assert resp.status_code == 201, resp.text
    assert resp.json()["ops"] == []


# --- 内置 SKILL ---


def test_read_skill_gives_the_full_text_and_rejects_unknown_names(
    client: TestClient, pid: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.ai import skills
    from app.core.errors import AppError

    for name in skills.NAMES:
        text = skills.render(name)
        assert "## 怎么写" in text and "## 范例" in text
        assert "non_diegetic_music" in text, "无配乐那条必须写在每一份里"

    with pytest.raises(AppError) as caught:
        skills.render("不存在的 skill")
    err = caught.value.to_dict()
    assert err["code"] == "VALIDATION_ERROR"
    assert err["title"] and err["detail"] and err["suggestions"]

    # 清单只有一行一份：它是唯一进系统提示词的部分
    assert len(skills.catalog().splitlines()) == len(skills.NAMES)

    use_fake_llm(
        monkeypatch,
        [
            {
                "content": "",
                # 不用 call()：这个工具的参数就叫 name，会和辅助函数的形参撞上
                "tool_calls": [{"id": "c1", "name": "read_skill", "arguments": {"name": "flf"}}],
            },
            {"content": "照 flf 写。", "tool_calls": []},
        ],
    )
    resp = client.post(f"{API}/projects/{pid}/director/chat", json={"message": "怎么写 prompt"})
    assert resp.status_code == 201, resp.text


def test_skill_pick_covers_every_frame_combination() -> None:
    from app.ai import skills

    assert skills.pick(True, True) == "flf"
    assert skills.pick(True, False) == "i2v"
    assert skills.pick(False, True) == "l2v"
    assert skills.pick(False, False, True) == "ref"
    assert skills.pick(False, False, False) == "ref", "一张参考图都没有也退化到 ref"


# --- 镜头级写工具 ---


def test_shot_tools_are_proposal_only_then_land_with_a_four_part_prompt(
    client: TestClient, pid: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """镜头级工具与幕级同一条边界：chat 之后库里镜头数不变，apply 之后才真落库。"""
    sid = scene(client, pid, "第一幕")
    use_fake_llm(
        monkeypatch,
        [
            {
                "content": "",
                "tool_calls": [
                    call(
                        "add_shot",
                        scene_id=sid,
                        title="雨中疾驰",
                        duration=5,
                        camera_motion="中景，缓慢推进",
                        visual_prompt="轿车在雨幕里疾驰，路灯拉出长长的光带",
                        audio_dialogue="雨声与轮胎摩擦水面的声音",
                        negative_prompt="模糊, 变形",
                        skill="i2v",
                        why="这一幕还没有开场镜头",
                    )
                ],
            },
            {"content": "提了一个镜头。", "tool_calls": []},
        ],
    )
    body = client.post(f"{API}/projects/{pid}/director/chat", json={"message": "补个镜头"}).json()
    ops = body["ops"]
    assert [o["op"] for o in ops] == ["add_shot"]
    assert ops[0]["target"] == "shot" and ops[0]["before"]["shot_count"] == 0
    assert client.get(f"{API}/projects/{pid}/scenes/{sid}").json()["shot_count"] == 0, (
        "提案不该落库"
    )

    after = ops[0]["after"]
    assert after["prompt"].startswith("[SHOT 1]")
    assert "Camera Motion: 中景，缓慢推进" in after["prompt"]
    assert "Visual Prompt: 轿车在雨幕里疾驰" in after["prompt"]
    assert "声音设计：" in after["prompt"], "无配乐硬约束由代码补，不靠模型自觉"
    assert "background music" in after["negative_prompt"]

    resp = client.post(f"{API}/projects/{pid}/director/apply", json={"ops": ops})
    assert resp.status_code == 201, resp.text
    assert resp.json()["count"] == 1

    shots = client.get(f"{API}/projects/{pid}/storyboard").json()[0]["shots"]
    assert len(shots) == 1
    shot = client.get(f"{API}/projects/{pid}/shots/{shots[0]['id']}").json()
    assert shot["title"] == "雨中疾驰" and shot["duration"] == 5
    assert shot["prompt"].startswith("[SHOT 1]")
    assert "background music" in shot["negative_prompt"]


def test_update_shot_keeps_the_segments_it_was_not_given(
    client: TestClient, pid: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """只给 visual_prompt 时，机位与对白不该被抹成默认值。"""
    sid = scene(client, pid, "第一幕")
    created = client.post(
        f"{API}/projects/{pid}/scenes/{sid}/shots",
        json={
            "title": "旧镜头",
            "prompt": (
                "[SHOT 1]\nCamera Motion: 特写，固定\n"
                "Visual Prompt: 旧的画面\nAudio / Dialogue: 老王：别追了"
            ),
        },
    ).json()

    use_fake_llm(
        monkeypatch,
        [
            {
                "content": "",
                "tool_calls": [
                    call(
                        "update_shot",
                        shot_id=created["id"],
                        visual_prompt="新的画面：雨点砸在挡风玻璃上",
                        why="原来的画面描述太空",
                    )
                ],
            },
            {"content": "改了一条。", "tool_calls": []},
        ],
    )
    ops = client.post(f"{API}/projects/{pid}/director/chat", json={"message": "重写画面"}).json()[
        "ops"
    ]
    prompt = ops[0]["after"]["prompt"]
    assert "Camera Motion: 特写，固定" in prompt, "没给的那一段要从原 prompt 里接着用"
    assert "Audio / Dialogue: 老王：别追了" in prompt
    assert "新的画面：雨点砸在挡风玻璃上" in prompt

    client.post(f"{API}/projects/{pid}/director/apply", json={"ops": ops})
    shot = client.get(f"{API}/projects/{pid}/shots/{created['id']}").json()
    assert "特写，固定" in shot["prompt"] and "老王：别追了" in shot["prompt"]


def test_delete_and_reorder_shots_go_through_the_same_review(
    client: TestClient, pid: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = scene(client, pid, "第一幕")
    ids = [
        client.post(f"{API}/projects/{pid}/scenes/{sid}/shots", json={"title": t}).json()["id"]
        for t in ("A", "B", "C")
    ]
    use_fake_llm(
        monkeypatch,
        [
            {
                "content": "",
                "tool_calls": [
                    call("delete_shot", shot_id=ids[1], why="这一镜和上一镜重复"),
                    call(
                        "reorder_shots",
                        scene_id=sid,
                        order=[ids[2], ids[0]],
                        why="先给全景再给特写",
                    ),
                ],
            },
            {"content": "两条。", "tool_calls": []},
        ],
    )
    ops = client.post(f"{API}/projects/{pid}/director/chat", json={"message": "理一下顺序"}).json()[
        "ops"
    ]
    assert [o["op"] for o in ops] == ["delete_shot", "reorder_shots"]
    titles = [s["title"] for s in client.get(f"{API}/projects/{pid}/storyboard").json()[0]["shots"]]
    assert titles == ["A", "B", "C"], "提案阶段一行都不该动"

    resp = client.post(f"{API}/projects/{pid}/director/apply", json={"ops": ops})
    assert resp.status_code == 201 and resp.json()["failed"] == []
    titles = [s["title"] for s in client.get(f"{API}/projects/{pid}/storyboard").json()[0]["shots"]]
    assert titles == ["C", "A"]


def test_set_shot_link_lands_a_transition(
    client: TestClient, pid: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = scene(client, pid, "第一幕")
    ids = [
        client.post(f"{API}/projects/{pid}/scenes/{sid}/shots", json={"title": t}).json()["id"]
        for t in ("A", "B")
    ]
    use_fake_llm(
        monkeypatch,
        [
            {
                "content": "",
                "tool_calls": [
                    call(
                        "set_shot_link",
                        from_shot_id=ids[0],
                        to_shot_id=ids[1],
                        mode="transition",
                        duration=1.5,
                        why="两镜之间画面接不上",
                    )
                ],
            },
            {"content": "一条。", "tool_calls": []},
        ],
    )
    ops = client.post(f"{API}/projects/{pid}/director/chat", json={"message": "加个转场"}).json()[
        "ops"
    ]
    assert ops[0]["target"] == "shot_link" and ops[0]["after"]["mode"] == "transition"

    resp = client.post(f"{API}/projects/{pid}/director/apply", json={"ops": ops})
    assert resp.status_code == 201, resp.text
    assert resp.json()["applied"][0]["mode"] == "transition"

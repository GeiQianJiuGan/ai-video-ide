"""AI 导演的两种自动化：免确认模式与「一键全流程」。

盯的是**边界**，不是「AI 拆得好不好」：

  1. **免确认模式只改「谁按下那一下」**：落库照旧走 `apply()` 那一份实现，
     关着（默认）时 `chat` 一行业务数据都不改；
  2. **一键全流程要免确认开着**：关着时是一条四要素错误，**不会偷偷落几十行数据**；
  3. **四步真的连起来**：后一步用的是前一步真落进库的 id——拆分镜那句话里带着幕 id，
     镜头里那几个人接到第二步刚建出来的形象上；
  4. **跳过不是失败，但必须说出来**：没配出图服务只写 `image_skipped`（素材照旧建成），
     `auto_image=False` 时确定性地摘掉 `image_prompt`，超出 `max_scenes` 的幕写进 `warnings`；
  5. **绝不悄悄盖掉工程里那份剧本原文**；半路断了也不把已经落库的东西说成没发生。

LLM 一律 monkeypatch 掉。这里的假模型照「第几步」答话，省掉真模型先读工具那一半——
工具循环本身由 `tests/test_director_agent.py` 盯着。
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.ai.llm import client as llm
from app.core.config import settings
from tests.conftest import error_of

API = "/api/v1"

#: 四步各自在提示词里的记号（`services/director.py` 的 `_AUTO_*` 四段话里写着）。
STAGES = ("第一步", "第二步", "第三步", "第四步")

#: 一章原文。内容不重要——第一步是让模型读它，读成什么样不是这个文件盯的事。
CHAPTER = "雨夜，阿岚回到老码头，用一把旧铜钥匙打开了三号仓库的门。" * 4


def call(op: str, seq: int, /, **args: Any) -> dict[str, Any]:
    """一条工具调用。**前两个参数位置传**——素材那几条的 `arguments` 里正有一个 `name`。"""
    return {"id": f"c{seq}", "name": op, "arguments": args}


def materials() -> list[dict[str, Any]]:
    """三样素材各一条，**都带 `image_prompt`**——要盯的正是「顺带出一张参考图」那一支。"""
    return [
        call(
            "add_character",
            1,
            name="阿岚",
            description="二十出头，褪色军绿夹克",
            image_prompt="二十出头的女性，褪色军绿夹克",
            skill="char_sheet",
            why="主角",
        ),
        call(
            "add_location",
            2,
            name="老码头",
            variant="雨夜",
            time_of_day="夜",
            description="铁锈缆桩，积水反光",
            image_prompt="雨夜的旧货运码头",
            skill="scene_simple",
            why="第一幕的地点",
        ),
        call(
            "add_prop",
            3,
            name="旧铜钥匙",
            description="黄铜，边缘磨亮",
            image_prompt="一把旧铜钥匙",
            skill="prop_ref",
            why="关键道具",
        ),
    ]


def fake_llm(
    monkeypatch: pytest.MonkeyPatch,
    *,
    scenes: tuple[str, ...] = ("雨夜的老码头",),
    shots: int = 2,
    boom: str = "",
) -> list[str]:
    """照「第几步」答话的假模型。返回它每一步**真正收到的那句话**（顺序就是四步的顺序）。

    `boom` 那一步直接抛，用来盯「中途断了怎么办」。
    """
    monkeypatch.setattr(settings, "llm_provider", "openai_compatible")
    monkeypatch.setattr(settings, "llm_model", "fake-model")
    monkeypatch.setattr(settings, "llm_base_url", "http://127.0.0.1:9/v1")
    asked: list[str] = []

    async def fake(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        said = next(str(m["content"]) for m in reversed(messages) if m["role"] == "user")
        stage = next((s for s in STAGES if s in said), "")
        if said not in asked:
            asked.append(said)
        if stage and stage == boom:
            raise RuntimeError("模型这一步没答上来")
        # 工具跑完那一轮只需收尾说一句话；第一步全程只说话，一条提案都不提。
        if stage == STAGES[0] or any(m["role"] == "tool" for m in messages):
            return {"content": f"（{stage or '收尾'}）好了。", "tool_calls": []}
        if stage == STAGES[1]:
            return {"content": "", "tool_calls": materials()}
        if stage == STAGES[2]:
            return {
                "content": "",
                "tool_calls": [
                    call(
                        "add_scene",
                        seq,
                        title=title,
                        summary="核心剧本里的一段",
                        time_of_day="夜",
                        location_name="老码头",
                        why="这一段是一个连续的时空段落",
                    )
                    for seq, title in enumerate(scenes)
                ],
            }
        if stage == STAGES[3]:
            found = re.search(r"scn_[0-9A-Z]+", said)
            assert found, f"第四步那句话里必须带着幕 id：{said}"
            return {"content": "", "tool_calls": shot_calls(found.group(0), shots)}
        return {"content": "好。", "tool_calls": []}

    monkeypatch.setattr(llm, "complete_tools", fake)
    return asked


def shot_calls(scene_id: str, count: int) -> list[dict[str, Any]]:
    """`character_names` 写的是**名字**：第二步已经真落库了，所以这里当场就能对上人。"""
    return [
        call(
            "add_shot",
            seq,
            scene_id=scene_id,
            title=f"第 {seq + 1} 镜",
            description="雨水顺着缆绳往下淌",
            duration=4,
            camera="中景",
            movement="固定",
            character_names=["阿岚"],
            camera_motion="固定机位",
            visual_prompt="雨夜码头，阿岚站在缆桩旁",
            audio_dialogue="雨声",
            skill="ref",
            why="这一幕要有人",
        )
        for seq in range(count)
    ]


def auto_on(monkeypatch: pytest.MonkeyPatch, **over: Any) -> None:
    """打开免确认模式。`over` 里的键按 `director_<key>` 覆盖设置页那一组。"""
    monkeypatch.setattr(settings, "director_auto_apply", True)
    for key, value in over.items():
        monkeypatch.setattr(settings, f"director_{key}", value)


def image_service_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """一个不需要真连的出图服务（照 `tests/test_images.py`）：入队只查「配没配」。"""
    monkeypatch.setattr(settings, "image_provider", "http_api")
    monkeypatch.setattr(settings, "image_base_url", "http://127.0.0.1:9001")


def store_script(client: TestClient, pid: str, text: str) -> None:
    resp = client.patch(f"{API}/projects/{pid}/story", json={"raw_text": text})
    assert resp.status_code == 200, resp.text


def run(client: TestClient, pid: str, **body: Any) -> Any:
    return client.post(f"{API}/projects/{pid}/director/autopilot", json=body)


def pause(client: TestClient, pid: str) -> None:
    assert client.post(f"{API}/projects/{pid}/queue/pause").status_code == 200


def one_op_llm(monkeypatch: pytest.MonkeyPatch, op: dict[str, Any]) -> None:
    """一句普通对话 → 一条提案。**每次 chat 都是同一个来回**，不是一串会用完的回合。"""
    monkeypatch.setattr(settings, "llm_provider", "openai_compatible")
    monkeypatch.setattr(settings, "llm_model", "fake-model")
    monkeypatch.setattr(settings, "llm_base_url", "http://127.0.0.1:9/v1")

    async def fake(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        if any(m["role"] == "tool" for m in messages):
            return {"content": "加好了。", "tool_calls": []}
        return {"content": "", "tool_calls": [op]}

    monkeypatch.setattr(llm, "complete_tools", fake)


# --- 免确认模式：只改「谁按下那一下」 ---


def test_auto_apply_lands_the_proposal_in_the_same_request(
    client: TestClient, pid: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """关着（默认）时提案一行都不落；开着时同一个请求里就落，并如实回一份落库回执。"""
    one_op_llm(monkeypatch, call("add_scene", 1, title="雨夜追车", why="缺一个动作段落"))

    off = client.post(f"{API}/projects/{pid}/director/chat", json={"message": "加一幕雨夜追车"})
    assert off.status_code == 201, off.text
    assert off.json()["auto_applied"] is False
    assert len(off.json()["ops"]) == 1
    assert client.get(f"{API}/projects/{pid}/scenes").json() == [], "关着时数据库一行不许动"

    monkeypatch.setattr(settings, "director_auto_apply", True)
    on = client.post(f"{API}/projects/{pid}/director/chat", json={"message": "加一幕雨夜追车"})
    assert on.status_code == 201, on.text
    body = on.json()

    assert body["auto_applied"] is True and body["count"] == 1
    assert body["failed"] == []
    assert [entry["op"] for entry in body["applied"]] == ["add_scene"]
    assert [s["title"] for s in client.get(f"{API}/projects/{pid}/scenes").json()] == ["雨夜追车"]


# --- 一键全流程：要免确认开着 ---


def test_autopilot_needs_the_no_confirm_switch(
    client: TestClient, pid: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """关着免确认时是一条四要素错误——**不会偷偷落几十行数据**。"""
    fake_llm(monkeypatch)
    store_script(client, pid, CHAPTER)

    resp = run(client, pid)
    assert resp.status_code == 422, resp.text
    err = error_of(resp)

    assert "免确认" in err["title"]
    assert any("设置页" in s for s in err["suggestions"])
    assert any("一句一句" in s for s in err["suggestions"]), "逐条审阅那条路必须还在"
    assert client.get(f"{API}/projects/{pid}/scenes").json() == []
    assert client.get(f"{API}/projects/{pid}/characters").json() == []


# --- 一键全流程：四步真的连起来 ---


def test_autopilot_runs_the_four_stages_and_lands_them(
    client: TestClient, pid: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """四步都真落库，**后一步用的是前一步真落进库的 id**，顺带排的图也进了队列。"""
    pause(client, pid)
    asked = fake_llm(monkeypatch)
    image_service_on(monkeypatch)
    auto_on(monkeypatch)
    store_script(client, pid, CHAPTER)

    resp = run(client, pid)
    assert resp.status_code == 201, resp.text
    body = resp.json()

    assert body["auto_apply"] is True and body["halted"] is False
    assert [s["stage"] for s in body["stages"]] == ["digest", "materials", "scenes", "shots"]
    assert [s["label"] for s in body["stages"][:3]] == ["核心剧本", "人物 / 地点 / 道具", "拆幕"]
    assert body["stages"][0]["count"] == 0, "第一步只读剧本说结论，一条提案都不提"
    assert body["script"] == {"chars": len(CHAPTER), "saved": False, "replaced": False}
    assert body["failed"] == [] and body["warnings"] == []

    names = client.get(f"{API}/projects/{pid}/characters").json()
    assert [c["name"] for c in names] == ["阿岚"]
    assert [x["name"] for x in client.get(f"{API}/projects/{pid}/locations").json()] == ["老码头"]
    assert [x["name"] for x in client.get(f"{API}/projects/{pid}/props").json()] == ["旧铜钥匙"]

    lanes = client.get(f"{API}/projects/{pid}/storyboard").json()
    assert [lane["title"] for lane in lanes] == ["雨夜的老码头"]
    assert len(lanes[0]["shots"]) == 2
    assert body["scenes"] == [{"id": lanes[0]["id"], "title": "雨夜的老码头"}]
    assert lanes[0]["id"] in asked[3], "第四步那句话里必须带着上一步真落进库的幕 id"

    shot = client.get(f"{API}/projects/{pid}/shots/{lanes[0]['shots'][0]['id']}").json()
    assert [c["character_name"] for c in shot["cast"]] == ["阿岚"], "镜头里那个人要接到刚建的形象上"

    assert body["images"] == {
        "configured": True,
        "auto": True,
        "queued": 3,
        "skipped": [],
        "dropped": 0,
    }
    jobs = client.get(f"{API}/projects/{pid}/queue").json()["jobs"]
    assert len(jobs) == 3
    assert all(str(job["label"]).startswith("生成图片素材：") for job in jobs), jobs


# --- 跳过不是失败，但必须说出来 ---


def test_autopilot_without_an_image_service_still_builds_the_materials(
    client: TestClient, pid: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没配出图服务时只写 `image_skipped`：**素材照旧建成**，图那一项走手动那条路。"""
    asked = fake_llm(monkeypatch)
    auto_on(monkeypatch)
    store_script(client, pid, CHAPTER)

    resp = run(client, pid)
    assert resp.status_code == 201, resp.text
    body = resp.json()

    assert body["images"]["configured"] is False
    assert body["images"]["queued"] == 0
    assert body["images"]["dropped"] == 0, "关的是出图服务，不是这几句「长什么样」"
    assert len(body["images"]["skipped"]) == 3
    assert all("素材已经建好了" in line for line in body["images"]["skipped"])
    assert "现在还没配出图服务" in asked[1], "第二步得先把这件事说清楚"
    assert client.get(f"{API}/projects/{pid}/queue").json()["jobs"] == []
    assert len(client.get(f"{API}/projects/{pid}/characters").json()) == 1


def test_autopilot_can_leave_the_images_out(
    client: TestClient, pid: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`auto_image=False` **确定性地**摘掉 image_prompt——不指望提示词那句「不要」自觉。"""
    asked = fake_llm(monkeypatch)
    image_service_on(monkeypatch)
    auto_on(monkeypatch)
    store_script(client, pid, CHAPTER)

    resp = run(client, pid, auto_image=False)
    assert resp.status_code == 201, resp.text
    body = resp.json()

    assert body["images"] == {
        "configured": True,
        "auto": False,
        "queued": 0,
        "skipped": [],
        "dropped": 3,
    }
    assert "不要" in asked[1]
    assert client.get(f"{API}/projects/{pid}/queue").json()["jobs"] == []
    assert len(client.get(f"{API}/projects/{pid}/characters").json()) == 1


def test_autopilot_caps_the_scenes_and_says_so(
    client: TestClient, pid: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """超出 `max_scenes` 的幕写进 `warnings`：幕照旧建成，只是这一趟不给它拆分镜。"""
    asked = fake_llm(monkeypatch, scenes=("雨夜的老码头", "三号仓库里"))
    auto_on(monkeypatch)
    store_script(client, pid, CHAPTER)

    resp = run(client, pid, max_scenes=1)
    assert resp.status_code == 201, resp.text
    body = resp.json()

    assert "最多 1 幕" in asked[2]
    titles = [s["title"] for s in client.get(f"{API}/projects/{pid}/scenes").json()]
    assert titles == ["雨夜的老码头", "三号仓库里"], "拆出来的幕一条都不许少落"
    assert len(body["scenes"]) == 1
    assert any("只给前 1 幕拆了分镜" in line for line in body["warnings"]), body["warnings"]
    lanes = client.get(f"{API}/projects/{pid}/storyboard").json()
    assert [len(lane["shots"]) for lane in lanes] == [2, 0]


# --- 剧本原文：绝不悄悄盖掉 ---


def test_autopilot_never_overwrites_the_stored_script(
    client: TestClient, pid: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """带来的原文与库里那份不一样时先问一声；勾了替换才真的换掉。"""
    fake_llm(monkeypatch)
    auto_on(monkeypatch)
    store_script(client, pid, CHAPTER)
    other = "清晨的渔市，阿岚把那把钥匙交给了一个陌生人。" * 3

    resp = run(client, pid, text=other)
    assert resp.status_code == 422, resp.text
    err = error_of(resp)
    assert any("替换" in s for s in err["suggestions"])
    assert any("不带文字" in s for s in err["suggestions"])
    assert client.get(f"{API}/projects/{pid}/story").json()["raw_text"] == CHAPTER
    assert client.get(f"{API}/projects/{pid}/characters").json() == [], "问都还没问就先落了数据"

    ok = run(client, pid, text=other, replace_script=True)
    assert ok.status_code == 201, ok.text
    assert ok.json()["script"] == {"chars": len(other), "saved": True, "replaced": True}
    assert client.get(f"{API}/projects/{pid}/story").json()["raw_text"] == other


def test_autopilot_without_any_script_says_where_to_paste(
    client: TestClient, pid: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """两边都空时说清剧本从哪里来，而不是让第一步空转一圈。"""
    fake_llm(monkeypatch)
    auto_on(monkeypatch)

    resp = run(client, pid)
    assert resp.status_code == 422, resp.text
    err = error_of(resp)

    assert any("贴进" in s for s in err["suggestions"])
    assert any("一句一句" in s for s in err["suggestions"])


# --- 半路断了：已经落库的不许说成没发生 ---


def test_autopilot_keeps_what_landed_when_a_step_breaks(
    client: TestClient, pid: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """第四步断了 → 201 + `stages[-1].error` + 一条 warning，前三步的数据一条不回滚。"""
    fake_llm(monkeypatch, boom=STAGES[3])
    auto_on(monkeypatch)
    store_script(client, pid, CHAPTER)

    resp = run(client, pid)
    assert resp.status_code == 201, resp.text
    body = resp.json()

    assert body["halted"] is True
    assert body["count"] > 0
    last = body["stages"][-1]
    assert last["stage"] == "shots" and "拆分镜" in last["label"]
    assert last["error"]["code"] == "LLM_UNAVAILABLE"
    assert last["error"]["suggestions"], "断在半路也要给四要素"
    assert any("这一步没做完" in line for line in body["warnings"]), body["warnings"]

    assert [c["name"] for c in client.get(f"{API}/projects/{pid}/characters").json()] == ["阿岚"]
    lanes = client.get(f"{API}/projects/{pid}/storyboard").json()
    assert [len(lane["shots"]) for lane in lanes] == [0], "断在第四步，这一幕就该是 0 镜"


def test_autopilot_breaking_before_anything_lands_is_a_clean_error(
    client: TestClient, pid: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一行业务数据都还没落就断了 → 干净的四要素错误，不回一张空回执让用户自己猜。"""
    fake_llm(monkeypatch, boom=STAGES[1])
    auto_on(monkeypatch)
    store_script(client, pid, CHAPTER)

    resp = run(client, pid)
    assert resp.status_code == 503, resp.text
    err = error_of(resp)

    assert err["code"] == "LLM_UNAVAILABLE"
    assert any("已经落进库" in s for s in err["suggestions"])
    assert client.get(f"{API}/projects/{pid}/characters").json() == []
    assert client.get(f"{API}/projects/{pid}/scenes").json() == []

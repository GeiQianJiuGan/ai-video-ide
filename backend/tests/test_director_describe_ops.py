"""AI 协作栏里那条「补一句描述」（`set_description` 提案 → `apply` 才落库）。

写工具永不落库那条边界不是这里新加的，但描述这一支多了一件事：**写哪一列由
`describe.target` 说**（形象上没有 `description` 列，那一句要落在账单真读的 `traits`
上），落库那边照 `after["field"]` patch，不再认一遍 kind。所以这里盯四件事：

  1. **`chat` 之后描述一个字都没变**——提案不是改动；
  2. **`apply` 只落 `op != "reject"` 的**，丢弃的那一条必须真的没发生；
  3. **六种 `target_kind` 各落一遍**：漏一种就是「AI 说改好了、库里没有」；
  4. **`add_character` 的 `description` 真的写进去了**——那一句以前直接掉进地里
     （`CHARACTER_FIELDS` 里没有这一列），是这一轮顺带修的回归点。

LLM 一律 monkeypatch 掉（照 `tests/test_director_agent.py::use_fake_llm`）。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.services.describe import DESC_TARGETS
from tests.conftest import upload_png
from tests.test_director_agent import call, use_fake_llm

API = "/api/v1"
SENTENCE = "褪色军绿夹克，短发，左颊一道旧疤"


def one_round(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, pid: str, *calls: dict[str, Any]
) -> list[dict[str, Any]]:
    """让假 LLM 提一批案，回提案列表。**跑完库里一行都不该变**，由调用方断言。"""
    use_fake_llm(
        monkeypatch,
        [{"content": "", "tool_calls": list(calls)}, {"content": "提完了。", "tool_calls": []}],
    )
    resp = client.post(f"{API}/projects/{pid}/director/chat", json={"message": "把缺的描述补一下"})
    assert resp.status_code == 201, resp.text
    return list(resp.json()["ops"])


def six_targets(client: TestClient, pid: str) -> dict[str, str]:
    """六种目标各造一行，回 `kind → id`。**形象给形象 id，变体给变体 id。**"""
    char = client.post(f"{API}/projects/{pid}/characters", json={"name": "阿岚"}).json()
    appearance = client.get(f"{API}/projects/{pid}/characters/{char['id']}/appearances").json()[0]
    loc = client.post(f"{API}/projects/{pid}/locations", json={"name": "城南旧宅"}).json()
    variant = client.post(
        f"{API}/projects/{pid}/locations/{loc['id']}/variants", json={"name": "雨夜"}
    ).json()
    prop = client.post(f"{API}/projects/{pid}/props", json={"name": "旧怀表"}).json()
    return {
        "asset": upload_png(client, pid, "upload", "alan.png"),
        "character": str(char["id"]),
        "appearance": str(appearance["id"]),
        "location": str(loc["id"]),
        "location_variant": str(variant["id"]),
        "prop": str(prop["id"]),
    }


def test_a_proposal_changes_nothing_until_the_user_says_yes(
    client: TestClient, pid: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    aid = upload_png(client, pid, "upload", "alan.png")
    ops = one_round(
        monkeypatch,
        client,
        pid,
        call(
            "set_description",
            target_kind="asset",
            target_id=aid,
            description=SENTENCE,
            why="这张图现在只有一个文件名",
        ),
    )
    assert [o["op"] for o in ops] == ["set_description"]
    assert ops[0]["after"]["description"] == SENTENCE
    assert ops[0]["after"]["field"] == "description"
    assert ops[0]["before"]["description"] in (None, ""), "Diff 左边是库里现在那一句"
    assert ops[0]["why"]

    # 关键断言：一行都没改
    row = client.get(f"{API}/projects/{pid}/assets").json()[0]
    assert row["description"] is None, "提案不是改动"

    # 采用之后才落
    resp = client.post(f"{API}/projects/{pid}/director/apply", json={"ops": ops})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["count"] == 1 and body["failed"] == []
    assert body["applied"][0]["field"] == "description"
    assert client.get(f"{API}/projects/{pid}/assets").json()[0]["description"] == SENTENCE


def test_a_rejected_description_really_does_not_happen(
    client: TestClient, pid: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = six_targets(client, pid)
    ops = one_round(
        monkeypatch,
        client,
        pid,
        call(
            "set_description", target_kind="asset", target_id=ids["asset"], description="留下这条"
        ),
        call(
            "set_description",
            target_kind="prop",
            target_id=ids["prop"],
            description="不该被写进去",
        ),
    )
    ops[1]["op"] = "reject"

    body = client.post(f"{API}/projects/{pid}/director/apply", json={"ops": ops}).json()
    assert body["count"] == 1 and body["failed"] == []
    kept = client.get(f"{API}/projects/{pid}/assets").json()[0]
    assert kept["description"] == "留下这条"
    props = client.get(f"{API}/projects/{pid}/props").json()
    assert props[0]["description"] in (None, ""), "被丢弃的那一条不该发生"


def test_every_target_kind_lands_on_the_column_the_prompt_reads(
    client: TestClient, pid: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """六种目标各落一遍。漏一种就是「AI 说改好了、库里没有」那类最难查的失败。"""
    ids = six_targets(client, pid)
    ops = one_round(
        monkeypatch,
        client,
        pid,
        *[
            call(
                "set_description",
                target_kind=kind,
                target_id=ids[kind],
                description=f"{kind} 的那一句",
                why="补描述",
            )
            for kind in DESC_TARGETS
        ],
    )
    assert len(ops) == len(DESC_TARGETS)

    body = client.post(f"{API}/projects/{pid}/director/apply", json={"ops": ops}).json()
    assert body["failed"] == [], body["failed"]
    assert body["count"] == len(DESC_TARGETS)
    landed = {a["target_kind"]: a["field"] for a in body["applied"]}
    assert landed["appearance"] == "traits", "形象上没有 description 列，写 notes 到不了模型手上"
    assert landed["asset"] == "description"

    assert (
        client.get(f"{API}/projects/{pid}/assets").json()[0]["description"]
        == "asset 的那一句"
    )
    chars = client.get(f"{API}/projects/{pid}/characters").json()
    assert chars[0]["description"] == "character 的那一句"
    appearance = client.get(f"{API}/projects/{pid}/appearances/{ids['appearance']}").json()
    assert appearance["traits"] == "appearance 的那一句"
    locs = client.get(f"{API}/projects/{pid}/locations").json()
    assert locs[0]["description"] == "location 的那一句"
    assert locs[0]["variants"][0]["description"] == "location_variant 的那一句"
    props = client.get(f"{API}/projects/{pid}/props").json()
    assert props[0]["description"] == "prop 的那一句"


def test_an_unknown_target_kind_is_a_four_element_failure_not_a_silent_skip(
    client: TestClient, pid: str
) -> None:
    """`apply` 直接收到一条认不出的（提案可以是旧的、手改的），要说清而不是静默丢。"""
    resp = client.post(
        f"{API}/projects/{pid}/director/apply",
        json={
            "ops": [
                {
                    "op": "set_description",
                    "after": {"target_kind": "不认识这种", "target_id": "x", "description": "y"},
                }
            ]
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["count"] == 0 and len(body["failed"]) == 1
    err = body["failed"][0]["error"]
    assert err["code"] == "VALIDATION_ERROR"
    assert err["title"] and err["detail"] and err["suggestions"]


def test_adding_a_character_finally_keeps_the_description(
    client: TestClient, pid: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回归：`add_character` 的 `description` 以前直接掉进地里（表里没有这一列）。"""
    ops = one_round(
        monkeypatch,
        client,
        pid,
        # `call()` 的第一个形参就叫 name，所以角色名这一条只能自己拼
        {
            "id": "cadd_character",
            "name": "add_character",
            "arguments": {"name": "阿岚", "description": SENTENCE, "why": "剧本里出现了这个人"},
        },
    )
    body = client.post(f"{API}/projects/{pid}/director/apply", json={"ops": ops}).json()
    assert body["failed"] == [], body["failed"]

    chars = client.get(f"{API}/projects/{pid}/characters").json()
    assert chars[0]["name"] == "阿岚"
    assert chars[0]["description"] == SENTENCE, "AI 写的那句设定必须真的存下来"

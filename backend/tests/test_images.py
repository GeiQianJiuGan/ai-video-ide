"""图片素材生成（第三条生成链的业务入口）。

盯的是这条链的四条边界，不是某家端的脾气（方言层由 `tests/test_image_providers.py` 盯）：

  1. **先账单再动手**：`plan()` 一行库都不改，缺什么在那里就说出来；
  2. **输入错误直接说清**：不认识的 `target_kind` → 422 四要素，素材行不存在 → 404；
  3. **素材图永不覆盖**：`land()` 让定妆图版本 +1，旧版本一条不删（硬约束 3 的同一口径）；
  4. **镜头首 / 末帧只进素材库**：落完之后 `Shot.first_frame_asset_id` **仍然是空**——
     「哪一张是首帧」只认用户按下去的那一下。

出图那一步不需要真连服务：这里只到「入队」为止（入队前先 `POST /queue/pause`，
pump 就不会真去连任何东西），`land()` 直接喂字节。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.generation.providers import presets
from app.persistence.models_cast import SheetVersion
from app.persistence.models_gen import Job
from app.persistence.models_story import Shot
from app.persistence.models_world import PropReference
from app.services.base import db_of, fetch, fetch_all
from app.services.cast import cast
from app.services.images import images
from app.services.story import story
from app.services.world import world
from tests.conftest import PNG_1PX, error_of
from tests.test_providers import GRAPH, with_declaration, write_preset

API = "/api/v1"


@pytest.fixture(autouse=True)
def image_service_on(client: TestClient) -> None:
    """一个不需要真连的图片服务：入队只查「配没配」，不出网。

    **必须挂在 `client` 之后**：应用启动时会 `app_settings.apply()` 一次，
    在它之前摆好的设置会被那一下冲掉。
    """
    settings.image_provider = "http_api"
    settings.image_base_url = "http://127.0.0.1:9001"
    settings.image_api_key = ""
    settings.image_model = ""
    settings.image_size = "1024x1024"


def pause(client: TestClient, pid: str) -> None:
    assert client.post(f"{API}/projects/{pid}/queue/pause").status_code == 200


async def appearance_of(pid: str, name: str = "阿岚") -> dict[str, Any]:
    char = await cast.create_character(pid, {"name": name})
    return (await cast.list_appearances(pid, char["id"]))[0]


async def shot_of(pid: str) -> dict[str, Any]:
    scene = await story.create_scene(pid, {"title": "第一幕", "prompt": "雨夜"})
    return await story.create_shot(pid, scene["id"], {"title": "推近"})


def fake_job(target_kind: str, target_id: str) -> Job:
    """`land()` 只读 `target_kind` / `target_id`，所以不必真入库一行。"""
    return Job(id="job_fake", shot_id=None, target_kind=target_kind, target_id=target_id)


# --- SKILL 清单：界面上那个下拉的文案只有这一份 ---


def test_skills_listing(client: TestClient, pid: str) -> None:
    resp = client.get(f"{API}/projects/{pid}/images/skills")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert [row["name"] for row in body["items"]] == ["char_sheet", "scene_simple", "prop_ref"]
    for row in body["items"]:
        # 前端只渲染，不在组件里抄第二份文案
        assert row["title"] and row["when"] and row["fixed"] and row["lead"]
    assert body["rule"]
    assert "四视图" in body["items"][0]["fixed"]


# --- 账单：只读 ---


async def test_plan_reads_only_and_shows_the_full_prompt(client: TestClient, pid: str) -> None:
    appearance = await appearance_of(pid)
    before = len(await fetch_all(db_of(pid), SheetVersion))

    resp = client.post(
        f"{API}/projects/{pid}/images/plan",
        json={
            "target_kind": "appearance",
            "target_id": appearance["id"],
            "prompt": "二十出头，褪色军绿夹克",
        },
    )
    assert resp.status_code == 200, resp.text
    bill = resp.json()

    assert bill["can_generate"] is True and bill["missing"] == []
    assert bill["skill"]["name"] == "char_sheet"  # 形象自动选四视图那一份
    assert bill["target_label"] == "角色 · 阿岚 · 默认形象 四视图"
    # 结构由 SKILL 补齐，用户那段话只填「长什么样」
    assert "四视图" in bill["prompt"] and "二十出头，褪色军绿夹克" in bill["prompt"]
    assert bill["user_text"] == "二十出头，褪色军绿夹克"
    assert "watermark" in bill["negative_prompt"]
    assert bill["lands"] and bill["asset_kind"] == "character_sheet"
    assert bill["provider"]["configured"] is True

    # 账单跑完，库里一行都没变
    assert len(await fetch_all(db_of(pid), SheetVersion)) == before
    assert await fetch_all(db_of(pid), Job) == []


def test_plan_says_what_is_missing_instead_of_failing(client: TestClient, pid: str) -> None:
    """没配图片服务时账单照样给得出来，缺什么写在 `missing[]` 里（绝不静默失败）。"""
    settings.image_provider = "none"
    char = client.post(f"{API}/projects/{pid}/characters", json={"name": "阿岚"}).json()
    appearance = client.get(f"{API}/projects/{pid}/characters/{char['id']}/appearances").json()[0]

    resp = client.post(
        f"{API}/projects/{pid}/images/plan",
        json={"target_kind": "appearance", "target_id": appearance["id"], "prompt": "军绿夹克"},
    )
    assert resp.status_code == 200, resp.text
    bill = resp.json()

    assert bill["can_generate"] is False
    assert len(bill["missing"]) == 1
    err = bill["missing"][0]
    assert err["code"] == "MISSING_CAPABILITY"
    assert err["title"] and err["detail"] and err["suggestions"]
    # 手动路径必须写出来（硬约束 2）
    assert any("上传" in s or "手动" in s for s in err["suggestions"])
    # 提示词照样拼好了：用户能先看清系统会补哪几句
    assert "四视图" in bill["prompt"]


# --- 输入错误 ---


def test_unknown_target_kind_is_a_four_element_error(client: TestClient, pid: str) -> None:
    resp = client.post(
        f"{API}/projects/{pid}/images/plan",
        json={"target_kind": "banana", "target_id": "x", "prompt": "y"},
    )
    assert resp.status_code == 422
    err = error_of(resp)
    assert err["code"] == "VALIDATION_ERROR"
    assert "appearance" in " ".join(err["suggestions"])


def test_missing_target_row_is_404(client: TestClient, pid: str) -> None:
    resp = client.post(
        f"{API}/projects/{pid}/images/plan",
        json={"target_kind": "prop", "target_id": "prp_nope", "prompt": "一把旧铜钥匙"},
    )
    assert resp.status_code == 404
    error_of(resp)


def test_unknown_skill_name_is_rejected(client: TestClient, pid: str) -> None:
    resp = client.post(
        f"{API}/projects/{pid}/images/plan",
        json={
            "target_kind": "prop",
            "target_id": "prp_nope",
            "prompt": "x",
            "skill": "char_sheet_v2",
        },
    )
    assert resp.status_code in (404, 422)
    error_of(resp)


# --- 出图那份预设：账单上就要说清它还能不能用 ---


def use_comfy_preset(name: str) -> None:
    """把出图那一族切到本机 ComfyUI 那条路（它是唯一 `wants_preset` 的协议）。"""
    settings.image_provider = "comfy_preset"
    settings.image_preset = name


async def test_plan_refuses_an_image_preset_that_is_gone(client: TestClient, pid: str) -> None:
    """指的那份图被删掉时**账单上就说**，不是按下生成才在队列里得到一条失败。"""
    appearance = await appearance_of(pid)
    use_comfy_preset("早就删了的图")

    resp = client.post(
        f"{API}/projects/{pid}/images/plan",
        json={"target_kind": "appearance", "target_id": appearance["id"], "prompt": "军绿夹克"},
    )
    assert resp.status_code == 200, resp.text
    bill = resp.json()

    assert bill["can_generate"] is False
    err = bill["missing"][0]
    assert err["code"] == "INVALID_WORKFLOW"
    assert "早就删了的图" in err["detail"]
    assert any("上传" in s or "手动" in s for s in err["suggestions"])
    # 入队那道门也认同一份判断（同一个 `_prepare`）
    enqueue = client.post(
        f"{API}/projects/{pid}/images/generate",
        json={"target_kind": "appearance", "target_id": appearance["id"], "prompt": "军绿夹克"},
    )
    assert enqueue.status_code == 400, enqueue.text
    error_of(enqueue)
    assert await fetch_all(db_of(pid), Job) == []


async def test_plan_refuses_an_image_preset_without_a_prompt(client: TestClient, pid: str) -> None:
    """标了声明却没有 AIVS_PROMPT：这份图在出图那一栏里也是不能用的。"""
    appearance = await appearance_of(pid)
    graph = with_declaration(GRAPH)
    write_preset("四视图-没提示词", {k: v for k, v in graph.items() if k != "3"})
    use_comfy_preset("四视图-没提示词")

    resp = client.post(
        f"{API}/projects/{pid}/images/plan",
        json={"target_kind": "appearance", "target_id": appearance["id"], "prompt": "军绿夹克"},
    )
    bill = resp.json()

    assert bill["can_generate"] is False
    assert bill["missing"][0]["code"] == "INVALID_WORKFLOW"
    assert "AIVS_PROMPT" in bill["missing"][0]["detail"]


async def test_plan_warns_when_the_image_preset_is_not_declared(
    client: TestClient, pid: str
) -> None:
    """没标 `AIVS_IMAGE` 照旧能出图（升级前配好的机器不许坏），但代价要说出来：
    这份图同时还留在 R2V / 首尾帧的候选里。标上之后那句话消失。"""
    appearance = await appearance_of(pid)
    body = {"target_kind": "appearance", "target_id": appearance["id"], "prompt": "军绿夹克"}

    write_preset("四视图-老的", GRAPH)
    use_comfy_preset("四视图-老的")
    bill = client.post(f"{API}/projects/{pid}/images/plan", json=body).json()
    assert bill["can_generate"] is True, "只是一条警告，不是门槛"
    assert any(presets.DECLARE_IMAGE in w for w in bill["warnings"])

    write_preset("四视图-新的", with_declaration(GRAPH))
    use_comfy_preset("四视图-新的")
    bill = client.post(f"{API}/projects/{pid}/images/plan", json=body).json()
    assert bill["can_generate"] is True
    assert not any(presets.DECLARE_IMAGE in w for w in bill["warnings"])


# --- 入队：同一张 job 表、同一个 pump ---


async def test_generate_enqueues_one_job(client: TestClient, pid: str) -> None:
    pause(client, pid)
    prop = await world.create_prop(pid, {"name": "旧铜钥匙"})

    resp = client.post(
        f"{API}/projects/{pid}/images/generate",
        json={"target_kind": "prop", "target_id": prop["id"], "prompt": "黄铜，边缘磨亮"},
    )
    assert resp.status_code == 201, resp.text
    job = resp.json()

    assert job["shot_id"] is None  # 素材图不属于任何镜头
    assert (job["target_kind"], job["target_id"]) == ("prop", prop["id"])
    assert job["kind"] == "t2i"  # 没带参考图
    assert job["target_label"] == "道具 · 旧铜钥匙 参考图"
    # 冻结的是**拼好之后**那两段：SKILL 之后改了，已入队的这一张也不该变样
    assert "纯白背景" in job["plan"]["prompt"]

    # 底部控制台那份清单认得出它是什么（否则 shot_title 一片空白）
    listed = client.get(f"{API}/projects/{pid}/queue").json()
    assert [row["target_label"] for row in listed["jobs"]] == ["道具 · 旧铜钥匙 参考图"]


def test_generate_without_service_says_how_to_go_on(client: TestClient, pid: str) -> None:
    settings.image_provider = "none"
    prop = client.post(f"{API}/projects/{pid}/props", json={"name": "旧铜钥匙"}).json()

    resp = client.post(
        f"{API}/projects/{pid}/images/generate",
        json={"target_kind": "prop", "target_id": prop["id"], "prompt": "黄铜"},
    )
    assert resp.status_code == 400, resp.text
    err = error_of(resp)
    assert err["code"] == "MISSING_CAPABILITY"
    assert any("图片生成 API" in s for s in err["suggestions"])


# --- 落地：素材图永不覆盖 ---


async def test_land_appends_a_sheet_version(client: TestClient, pid: str) -> None:
    """定妆图版本 +1，旧版本一条不删——只是不再是「当前」。"""
    appearance = await appearance_of(pid)
    await images.land(pid, fake_job("appearance", appearance["id"]), "a.png", PNG_1PX)
    second = await images.land(pid, fake_job("appearance", appearance["id"]), "b.png", PNG_1PX)

    rows = await fetch_all(
        db_of(pid), SheetVersion, where=SheetVersion.appearance_id == appearance["id"]
    )
    assert sorted(r.version_no for r in rows) == [1, 2]
    assert [r.version_no for r in rows if r.is_current] == [2]
    assert second["target_kind"] == "appearance"
    assert second["hint"] == ""  # 素材图是自动挂上去的，没有「还要点一下」这回事


async def test_land_appends_a_prop_reference(client: TestClient, pid: str) -> None:
    prop = await world.create_prop(pid, {"name": "旧铜钥匙"})
    await images.land(pid, fake_job("prop", prop["id"]), "a.png", PNG_1PX)
    await images.land(pid, fake_job("prop", prop["id"]), "b.png", PNG_1PX)

    rows = await fetch_all(db_of(pid), PropReference, where=PropReference.prop_id == prop["id"])
    assert len(rows) == 2


async def test_shot_frame_only_enters_the_library(client: TestClient, pid: str) -> None:
    """**这条是「首帧只认用户按下去那一下」的守卫**：图进了素材库，槽位一个字节都没动。"""
    shot = await shot_of(pid)
    landed = await images.land(pid, fake_job("shot_first_frame", shot["id"]), "f.png", PNG_1PX)

    row = await fetch(db_of(pid), Shot, shot["id"], "镜头")
    assert row.first_frame_asset_id is None
    assert row.last_frame_asset_id is None
    assert "还没有设成首帧" in landed["hint"]
    assert landed["asset_id"] and landed["asset_path"]

    # 用户显式点一下才写槽位（走已有的 `ShotPatch`，这里不新增写路径）
    patched = client.patch(
        f"{API}/projects/{pid}/shots/{shot['id']}",
        json={"first_frame_asset_id": landed["asset_id"]},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["first_frame_asset_id"] == landed["asset_id"]


async def test_generated_file_lands_in_the_project_dir(
    client: TestClient, pid: str, project_dir: Path
) -> None:
    appearance = await appearance_of(pid)
    landed = await images.land(pid, fake_job("appearance", appearance["id"]), "a.png", PNG_1PX)

    # `path` 相对工程目录存（整个目录拷走仍然有效）
    rel = Path(str(landed["asset_path"]))
    assert not rel.is_absolute()
    assert (project_dir / rel).read_bytes() == PNG_1PX


# --- 队列面板要的那句话 ---


async def test_target_label_never_raises(client: TestClient, pid: str) -> None:
    """认不出来回 `None`：队列列表不该因为一行素材被删了就整个 500。"""
    assert await images.target_label(pid, None, None) is None
    assert await images.target_label(pid, "banana", "x") is None
    assert await images.target_label(pid, "prop", "prp_gone") is None

"""工程路由：**「这个工程 + 这个能力 → 走哪条路出片」只有一份口径。**

以前这件事没有口径：入队时写死 `generation_mode = "comfy_preset"`、执行时写死
`registry.provider("comfy_preset")`，`project.generation_mode` 是一列**只写值**——界面上选了
「通用 REST API」或「ComfyUI 工作流绑定」，后端照旧提交给 ComfyUI 预设，选了等于没选，
而冻结进版本的参数里还写着用户选的那条路（破硬约束 3、4）。

这里钉住六件事：

  1. **工程那一列为空 = 跟随设置页**，不是「没配置」；「这个答案是谁给的」（`source`）
     要说得出来，否则用户改了设置页也不知道哪些工程会跟着变；
  2. **一条路只有一个名字**：老名字 `workflow_api` 读写两侧都归一到 `comfy_workflow`，
     编出来的名字**当场报错而不是静默回退到默认那条**（那正是这次要修的 bug 的形状）；
  3. 三条路各自「缺什么」都是四要素（用 `conftest.py::error_of` 那把尺子量），且
     **同一个工程的两条能力可以给出两个不同答案**——readiness 是按能力算的；
  4. 参考素材槽位按**真正会提交的那一份**数：R2V 与首尾帧可以是两份图、两个数，
     REST 那条不限量，绑定那条只喂图片（视频 / 音频是 0，不是「不知道」）；
  5. 这个镜头要哪种能力只有一处判断（有上游 = 首尾帧，调用方显式给的 `kind` 优先）；
  6. **入队那道门槛在入队之前**：缺地址就是一个四要素错误，队列里一条都不许留；
     解析出来的那条路整个冻结进任务参数，重试只读冻结值。

只读那一条（`GET /projects/{pid}/route`）**绝不抛**：用户正是来这儿看哪里不对的，
所以这里断言的是 `issues` 的内容，而不是一个 5xx。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.errors import AppError, ErrorCode
from app.persistence.models_story import Shot
from app.services import route
from app.services.generation import _provider_of
from tests.conftest import error_of, import_workflow, ready_workflow, write_preset

API = "/api/v1"

#: REST 那条路上的地址。测试永远不会真的连它——入队一律先 `POST /queue/pause`。
BASE = "http://127.0.0.1:9/v1"

#: 拿来证明「密钥一次都不经过路由」的那个字符串。
SECRET = "sk-route-secret"


# --- 公共脚手架 ---


def route_of(client: TestClient, pid: str) -> dict[str, Any]:
    """`GET /projects/{pid}/route`：概览页那一块一个请求画完，缺什么也在里面。"""
    resp = client.get(f"{API}/projects/{pid}/route")
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


def caps_of(client: TestClient, pid: str) -> dict[str, dict[str, Any]]:
    """两条能力按名字取：普通镜头（`image2video`）与衔接转场（`first_last_frame`）。"""
    return {c["capability"]: c for c in route_of(client, pid)["capabilities"]}


def four_elements(issue: dict[str, Any]) -> dict[str, Any]:
    """把 `Route.issues` 里的一条拿给 conftest 那把尺子量。

    `issues` 存的就是 `AppError.to_dict()`——只读路径不抛，所以它进的是响应体而不是异常。
    套一层 `json()` 让「四要素齐全」这件事仍然只有一份判断（`error_of`）。
    """
    return error_of(SimpleNamespace(json=lambda: {"error": issue}))


def set_mode(client: TestClient, pid: str, mode: str) -> dict[str, Any]:
    """调用方式只有这一个写入口（`PUT /workflow-bindings`，过 `route.normalize()`）。"""
    resp = client.put(f"{API}/projects/{pid}/workflow-bindings", json={"generation_mode": mode})
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


def patch_settings(client: TestClient, values: dict[str, Any]) -> None:
    """走真正的设置页那条路（落 settings.json）。

    刻意不 monkeypatch `settings` 单例：`TestClient` 起 lifespan 时会再 `apply()` 一遍，
    没写进文件的覆盖会被擦回默认值。
    """
    resp = client.patch(f"{API}/settings", json=values)
    assert resp.status_code == 200, resp.text


def _node(class_type: str, title: str, **inputs: Any) -> dict[str, Any]:
    return {"class_type": class_type, "inputs": inputs, "_meta": {"title": title}}


def preset_graph(*, refs: int = 0, flf: bool = True) -> dict[str, Any]:
    """按需要摆一份预设图：`AIVS_PROMPT` 必给，首尾帧入口与参考图槽位按参数给。

    `flf=False` 就是「只标了提示词」那种图：普通镜头能跑、补转场跑不了——同一个工程上
    两条能力于是给出两个不同的答案，这正是 readiness 必须按能力算的理由。
    """
    graph: dict[str, Any] = {"1": _node("CLIPTextEncode", "AIVS_PROMPT", text="")}
    if flf:
        graph["2"] = _node("LoadImage", "AIVS_FIRST_FRAME", image="first.png")
        graph["3"] = _node("LoadImage", "AIVS_LAST_FRAME", image="last.png")
    for i in range(1, refs + 1):
        graph[str(10 + i)] = _node("LoadImage", f"AIVS_REF_{i}", image=f"ref{i}.png")
    return graph


def bound_workflow(client: TestClient, pid: str, *, slots: list[str]) -> dict[str, Any]:
    """导入一份标了 N 个参考图槽位的绑定图并校验通过（probe=false，不需要 ComfyUI）。"""
    row = import_workflow(
        client,
        pid,
        "image2video",
        name="带槽位的绑定图",
        bindings={
            "prompt": "6.text",
            "reference_image": slots[0],
            "reference_image_slots": slots,  # type: ignore[dict-item]
        },
    )
    resp = client.post(f"{API}/projects/{pid}/workflows/{row['id']}/validate?probe=false")
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True, resp.text
    return dict(client.get(f"{API}/projects/{pid}/workflows/{row['id']}").json())


def bind(client: TestClient, pid: str, wid: str) -> None:
    """把这份图绑到工程的 `image2video` 上，同时把路切到绑定那条。"""
    resp = client.put(
        f"{API}/projects/{pid}/workflow-bindings",
        json={"generation_mode": "comfy_workflow", "image2video": wid},
    )
    assert resp.status_code == 200, resp.text


def pause(client: TestClient, pid: str) -> None:
    """入队前必须先暂停，否则 pump 会真的去敲后端。"""
    assert client.post(f"{API}/projects/{pid}/queue/pause").status_code == 200


def make_shot(client: TestClient, pid: str) -> str:
    """一幕 + 一个镜头，回镜头 id。上下文是另一道门槛，这里一律 `check_context=False`。"""
    scene = client.post(f"{API}/projects/{pid}/scenes", json={"title": "第一场"})
    assert scene.status_code == 201, scene.text
    resp = client.post(
        f"{API}/projects/{pid}/scenes/{scene.json()['id']}/shots",
        json={"title": "推近", "prompt": "雨夜，林昭推门"},
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def generate(client: TestClient, pid: str, shot_id: str) -> Any:
    return client.post(
        f"{API}/projects/{pid}/shots/{shot_id}/generate", json={"check_context": False}
    )


def jobs_of(client: TestClient, pid: str) -> list[dict[str, Any]]:
    resp = client.get(f"{API}/projects/{pid}/jobs")
    assert resp.status_code == 200, resp.text
    return list(resp.json())


# --- 一、继承与 source：工程那一列为空 = 跟随设置页 ---


def test_an_empty_project_column_follows_the_settings_page(client: TestClient, pid: str) -> None:
    """空 ≠ 没配置。**绝大多数工程是这一种**，所以这条路要一路说清答案是谁给的。"""
    body = route_of(client, pid)
    assert body["mode"] == "", "新工程不该替用户先选一条路"
    assert body["provider"] == "comfy_preset"
    assert body["source"] == "default", "谁都没选过：用的是代码里那个默认值"
    assert body["settings_provider"] == "comfy_preset", "「留空会走哪条」也要写出来"
    assert body["binds"] == "preset" and body["binds_workflow"] is False

    # 设置页改成通用 REST API：工程那一列一个字没动，路却跟着换了
    patch_settings(client, {"video.provider": "http_api", "video.base_url": BASE})
    body = route_of(client, pid)
    assert (body["mode"], body["provider"], body["source"]) == ("", "http_api", "settings")
    assert body["binds"] == "base_url", "画哪一组控件照事实分岔，不照调用方式的名字"

    # 工程显式选一条：从此不再跟着设置页走
    assert set_mode(client, pid, "comfy_preset")["generation_mode"] == "comfy_preset"
    body = route_of(client, pid)
    assert (body["mode"], body["provider"], body["source"]) == (
        "comfy_preset",
        "comfy_preset",
        "project",
    )
    assert body["settings_provider"] == "http_api", "设置页那条照旧写出来：清空就回到它"

    # 清回空串 = 重新跟随设置页，而不是「没配置」
    assert set_mode(client, pid, "")["generation_mode"] == ""
    assert route_of(client, pid)["source"] == "settings"


def test_the_options_list_offers_follow_the_settings_page_first(
    client: TestClient, pid: str
) -> None:
    """前端一个调用方式的名字都不写死：候选、标签、两条能力的中文名全部由后端给。"""
    body = route_of(client, pid)
    assert body["options"][0] == {
        "name": "",
        "label": "跟随设置页",
        "inherit": True,
        "binds": "",
    }
    rest = body["options"][1:]
    assert [o["name"] for o in rest] == ["comfy_preset", "http_api", "comfy_workflow"]
    assert all(o["inherit"] is False for o in rest)
    assert all(o["legacy"] is False for o in rest), "三条路一视同仁，没有「兼容选项」"
    assert all(o["label"] for o in rest)
    #: 「要改成哪一条才会绑图」得由后端回答，否则 `comfy_workflow` 又写死进前端了。
    assert [o["binds"] for o in rest] == ["preset", "base_url", "workflow"]
    assert [c["capability_label"] for c in body["capabilities"]] == [
        "普通镜头（图生视频）",
        "衔接与转场（首尾帧）",
    ]
    assert any("mode=refine" in line for line in body["contract"]), (
        "服务端要实现什么由后端给：写死在前端的话，改合同就得改两处"
    )


def test_the_app_level_default_preset_has_one_grid_per_role(client: TestClient, pid: str) -> None:
    """「跟随设置页」跟的是**按角色那一格**，留空才退回共用那一格。

    以前应用级只有一个格子（`video.preset`），而两个角色要的入口本来就不一样（R2V 只要
    `AIVS_PROMPT`，首尾帧还要两头的帧），一台机器上常常是两份不同的图——于是「工程没绑就
    跟随设置页」在首尾帧上必然落到一份不能用的图上，用户只能回到每个工程里各绑一次。
    """
    shared = write_preset("共用那份", preset_graph())
    r2v = write_preset("只有提示词那份", preset_graph(flf=False))
    flf = write_preset("首尾帧那份", preset_graph())

    # 只有共用那一格：两条能力都跟它
    patch_settings(client, {"video.preset": shared})
    assert route.app_preset_of("image2video") == shared
    assert route.app_preset_of("first_last_frame") == shared
    caps = caps_of(client, pid)
    assert [c["preset"] for c in caps.values()] == [shared, shared]

    # 按角色各指一份：两条能力分别落到自己那一格，共用那一格不再参与
    patch_settings(client, {"video.r2v_preset": r2v, "video.flf_preset": flf})
    caps = caps_of(client, pid)
    assert caps["image2video"]["preset"] == r2v
    assert caps["first_last_frame"]["preset"] == flf
    assert [c["ready"] for c in caps.values()] == [True, True]
    assert [c["source"] for c in caps.values()] == ["default", "default"], (
        "工程那一列一个字都没动：这几项是预设名，不是调用方式"
    )
    for cap in route.FLF_CAPABILITIES:
        assert route.app_preset_of(cap) == flf, "转场与 FL2VA 与首尾帧同一格"

    # 按角色那一项留空 = 退回共用那份（只有一份图的人什么都不用配）
    patch_settings(client, {"video.flf_preset": ""})
    assert route.app_preset_of("first_last_frame") == shared
    assert caps_of(client, pid)["first_last_frame"]["preset"] == shared

    # 三格全空 = 真的没有默认，这时候才是那句四要素错误
    patch_settings(client, {"video.preset": "", "video.r2v_preset": ""})
    assert route.app_preset_of("image2video") is None
    caps = caps_of(client, pid)
    assert [c["ready"] for c in caps.values()] == [False, False]
    assert four_elements(caps["image2video"]["issues"][0])["title"] == "还没有选生成预设"


def test_a_new_project_does_not_materialize_the_app_default(client: TestClient, pid: str) -> None:
    """新建工程时**不把当时的应用级默认抄进库**——抄过一次就再也跟不上了。

    账单上那一格于是必须有 `app` 这一级：少了它界面会说「没选预设」而按下生成却成功，
    那正是硬约束 4 要防的形状。
    """
    name = write_preset("设置页那份", preset_graph())
    patch_settings(client, {"video.preset": name})

    bindings = client.get(f"{API}/projects/{pid}/workflow-bindings")
    assert bindings.status_code == 200, bindings.text
    body = dict(bindings.json())
    assert body["generation_mode"] == "", "调用方式那一列：空 = 跟随设置页"

    shot_id = make_shot(client, pid)
    resp = client.get(f"{API}/projects/{pid}/shots/{shot_id}/params")
    assert resp.status_code == 200, resp.text
    cell = resp.json()["fields"]["preset"]
    assert (cell["value"], cell["level"]) == (name, "app"), (
        "账单不能比事实少一级：这一格现在是绝大多数工程真正用的那一份"
    )

    # 换一份默认，同一个工程跟着变（物化进库的话这里还会是上面那份）
    other = write_preset("换成这份", preset_graph())
    patch_settings(client, {"video.r2v_preset": other})
    cell = client.get(f"{API}/projects/{pid}/shots/{shot_id}/params").json()["fields"]["preset"]
    assert (cell["value"], cell["level"]) == (other, "app")

    # 工程自己绑一份之后就不再跟：那一级在账单上是 `project`
    mine = write_preset("这个工程自己那份", preset_graph())
    assert client.put(f"{API}/projects/{pid}/preset", json={"r2v_name": mine}).status_code == 200
    cell = client.get(f"{API}/projects/{pid}/shots/{shot_id}/params").json()["fields"]["preset"]
    assert (cell["value"], cell["level"]) == (mine, "project")


# --- 二、一条路只有一个名字 ---


def test_one_route_has_exactly_one_name() -> None:
    """`workflow_api` 是 `comfy_workflow` 的老名字；**编出来的名字当场报错**。

    静默回退到默认那条正是这次要修的 bug 的形状：选了等于没选，而用户看不出来。
    """
    assert route.normalize(None) == route.INHERIT == ""
    assert route.normalize("   ") == "", "留空 = 跟随设置页"
    assert route.normalize("workflow_api") == "comfy_workflow"
    assert route.normalize("comfy_workflow") == "comfy_workflow"

    with pytest.raises(AppError) as caught:
        route.normalize("wan")
    err = caught.value
    assert err.code is ErrorCode.VALIDATION_ERROR
    assert err.status_code == 422, "写入侧该挡就挡"
    assert four_elements(err.to_dict())["title"] == "不认识这个调用方式"
    assert err.related_ids["available"] == ["comfy_preset", "http_api", "comfy_workflow"]
    assert any("跟随设置页" in s for s in err.suggestions), "留空那条出路必须写出来"


def test_the_old_name_is_normalized_on_both_sides(client: TestClient, pid: str) -> None:
    """老客户端写 `workflow_api`，读回来必须是 registry 那个名字。

    同一条路两个名字、中间没有映射时，前端拿它和候选比对不上——界面上那个下拉会显示成
    「没选」，而库里明明选着。
    """
    assert set_mode(client, pid, "workflow_api")["generation_mode"] == "comfy_workflow"
    assert route_of(client, pid)["mode"] == "comfy_workflow"
    bindings = client.get(f"{API}/projects/{pid}/workflow-bindings")
    assert bindings.status_code == 200, bindings.text
    assert bindings.json()["generation_mode"] == "comfy_workflow"

    resp = client.put(f"{API}/projects/{pid}/workflow-bindings", json={"generation_mode": "wan"})
    assert resp.status_code == 422, resp.text
    assert error_of(resp)["title"], "未知调用方式在写入侧就挡住，不许静默丢弃"
    assert route_of(client, pid)["mode"] == "comfy_workflow", "挡住了就一个字都没写进去"


# --- 三、三条路各自「缺什么」都是四要素 ---


def test_the_preset_route_names_which_preset_is_missing(client: TestClient, pid: str) -> None:
    """预设那条路：没选是一句话，选了但那份图不能用于这个能力是另一句话。"""
    caps = caps_of(client, pid)
    assert [c["ready"] for c in caps.values()] == [False, False]
    err = four_elements(caps["image2video"]["issues"][0])
    assert err["code"] == "MISSING_CAPABILITY"
    assert err["title"] == "还没有选生成预设"
    assert any("概览页" in s for s in err["suggestions"])
    assert any("设置页" in s for s in err["suggestions"]), "跟随设置页那条出路也要写出来"
    assert any("设为 R2V 默认" in s for s in err["suggestions"]), (
        "「去哪儿设那份默认」要点名到角色：只说「选一份默认预设」的话，用户在共用那一格里"
        "配了一份 R2V 图，首尾帧照旧报同一句错"
    )
    flf_err = four_elements(caps["first_last_frame"]["issues"][0])
    assert any("设为首尾帧默认" in s for s in flf_err["suggestions"])

    # 只标了 AIVS_PROMPT 的那种图：普通镜头能跑，补转场跑不了
    name = write_preset("只有提示词", preset_graph(flf=False))
    patch_settings(client, {"video.preset": name})
    caps = caps_of(client, pid)
    assert caps["image2video"]["ready"] is True
    assert caps["image2video"]["preset"] == name, "解析到具体那一份，不是「设置页那份」"
    assert caps["image2video"]["issues"] == []
    flf = caps["first_last_frame"]
    assert flf["ready"] is False, "同一个工程，两条能力两个答案"
    err = four_elements(flf["issues"][0])
    assert err["code"] == "INVALID_WORKFLOW"
    assert err["title"] == "预设不可用"
    assert any("AIVS_FIRST_FRAME" in s for s in err["suggestions"]), "缺哪个入口要说清"


def test_the_rest_route_names_the_address_and_what_the_server_must_implement(
    client: TestClient, pid: str
) -> None:
    """REST 那条路：缺的是地址，而「服务端要实现什么」和它是同一句话。"""
    set_mode(client, pid, "http_api")
    cap = caps_of(client, pid)["image2video"]
    assert cap["ready"] is False and cap["binds"] == "base_url"
    assert cap["binds_workflow"] is False, "「这条路不需要工作流绑定」就是这一个布尔"
    err = four_elements(cap["issues"][0])
    assert err["code"] == "MISSING_CAPABILITY"
    assert err["title"] == "还没有配置视频生成服务地址"
    assert any("视频生成 API" in s for s in err["suggestions"])
    assert any("/submit" in s for s in err["suggestions"]), "服务端要实现什么，一起说"

    patch_settings(client, {"video.base_url": BASE + "/", "video.api_key": SECRET})
    body = route_of(client, pid)
    cap = {c["capability"]: c for c in body["capabilities"]}["image2video"]
    assert cap["ready"] is True and cap["issues"] == []
    assert cap["base_url"] == BASE, "末尾那个斜杠在这里收掉，界面与冻结的参数才对得上"
    assert cap["preset"] is None and cap["workflow_id"] is None
    assert SECRET not in json.dumps(body, ensure_ascii=False), "密钥一次都不经过这条路"


def test_the_binding_route_names_the_capability_that_has_no_graph(
    client: TestClient, pid: str
) -> None:
    """绑定那条路：一份图只给一个能力，所以两条能力可以一条绑上、一条没绑上。"""
    set_mode(client, pid, "comfy_workflow")
    err = four_elements(caps_of(client, pid)["image2video"]["issues"][0])
    assert err["code"] == "MISSING_CAPABILITY"
    assert err["title"] == "能力「image2video」不可用", "一张图都还没导入时先说这句"
    assert err["detail"], "「缺了会怎样」那句由 workflows.resolve 给，这里不另写一份"

    wf = ready_workflow(client, pid, "image2video")
    bind(client, pid, wf["id"])
    caps = caps_of(client, pid)
    bound = caps["image2video"]
    assert bound["ready"] is True and bound["binds"] == "workflow"
    assert bound["binds_workflow"] is True
    assert (bound["workflow_id"], bound["workflow_name"]) == (wf["id"], wf["name"])
    assert bound["preset"] is None and bound["base_url"] is None
    assert bound["slots"]["image"] == 0, "这份图一个参考图槽位都没标：0 是事实，不是「没查到」"
    assert "一个参考图槽位都没有" in bound["slots"]["detail"]

    flf = caps["first_last_frame"]
    assert flf["ready"] is False
    err = four_elements(flf["issues"][0])
    assert err["title"] == "项目未绑定「first_last_frame」Workflow"


# --- 四、参考素材槽位按真正会提交的那一份数 ---


def test_capacity_counts_the_preset_that_will_actually_be_submitted(
    client: TestClient, pid: str
) -> None:
    """R2V 与首尾帧可以是两份图、两个数。

    以前这里一律去数 R2V 那一份：首尾帧镜头的「会丢几张」是照一份根本不会被提交的图算的，
    账单上那个「超出会丢几个」的确认于是也是假的。
    """
    r2v = write_preset("两槽的图", preset_graph(refs=2, flf=False))
    flf = write_preset("五槽的首尾帧图", preset_graph(refs=5))
    resp = client.put(f"{API}/projects/{pid}/preset", json={"r2v_name": r2v, "flf_name": flf})
    assert resp.status_code == 200, resp.text

    caps = caps_of(client, pid)
    assert [c["ready"] for c in caps.values()] == [True, True]
    assert caps["image2video"]["preset"] == r2v
    assert caps["image2video"]["slots"]["image"] == 2
    assert caps["image2video"]["slots"]["source"] == r2v, "这个数字是哪份图给的要写出来"
    assert caps["first_last_frame"]["preset"] == flf
    assert caps["first_last_frame"]["slots"]["image"] == 5


def test_capacity_is_unlimited_on_rest_and_image_only_on_the_binding_route(
    client: TestClient, pid: str
) -> None:
    """REST 那条整组发过去（没有槽位这回事），绑定那条只喂图片。"""
    patch_settings(client, {"video.provider": "http_api", "video.base_url": BASE})
    slots = caps_of(client, pid)["image2video"]["slots"]
    assert slots["image"] is None, "凭空造一个上限只会白丢用户的角色图"
    assert (slots["video"], slots["audio"]) == (None, None)
    assert slots["source"] == "REST 合同" and slots["detail"]

    wf = bound_workflow(client, pid, slots=["10.image", "11.image"])
    bind(client, pid, wf["id"])
    slots = caps_of(client, pid)["image2video"]["slots"]
    assert slots["image"] == 2, "几张取决于绑定表里标了几个参考图槽位"
    assert (slots["video"], slots["audio"]) == (0, 0), "这条路只喂图片：0 是有意义的答案"
    assert "只喂图片" in slots["detail"]
    assert slots["source"] == wf["name"]


# --- 五、这个镜头要哪种能力 ---


def test_which_capability_a_shot_needs_has_exactly_one_implementation() -> None:
    """有上游 = 首尾帧；调用方显式给的 `kind` 优先（补转场、编排那条链、二次处理）。

    以前这行判断长在 `enqueue_shot` 里，而参考素材账单那侧默认按 R2V 数槽位——于是首尾帧
    镜头的账单数的是 R2V 那份预设、真正提交的却是 FLF 那份图，两个数字对不上。
    """
    assert route.capability_of(Shot(prev_shot_id=None)) == "image2video"
    assert route.capability_of(Shot(prev_shot_id="sht_1")) == "first_last_frame"
    assert route.capability_of(Shot(prev_shot_id="sht_1"), "image2video") == "image2video"
    assert route.capability_of(Shot(prev_shot_id=None), "transition") == "transition"

    # 转场与 FL2VA 都是「两头都给帧」，所以预设取 flf 那一份、绑定表落到首尾帧那一格
    assert {route.preset_role(c) for c in route.FLF_CAPABILITIES} == {"flf"}
    assert route.preset_role("image2video") == "r2v"
    assert route.WORKFLOW_CAPABILITY["transition"] == "first_last_frame"
    assert route.WORKFLOW_CAPABILITY["fl2va"] == "first_last_frame"


# --- 六、入队那道门槛，与冻结进任务参数的那一份 ---


def test_nothing_enters_the_queue_before_the_route_is_bound(client: TestClient, pid: str) -> None:
    """门槛在入队而不在执行。

    以前缺地址 / 缺预设 / 没绑图统统要等 pump 跑到那一条才炸：用户按十次生成就在队列里躺
    十条失败，而错误里只剩「ComfyUI 未连接」这种与真正原因差一层的话。
    """
    pause(client, pid)
    shot_id = make_shot(client, pid)
    set_mode(client, pid, "http_api")  # 地址那一栏是空的

    resp = generate(client, pid, shot_id)
    # `MISSING_CAPABILITY` 映射到 400（`core/errors.py::_STATUS`）。要紧的不是这个数字，
    # 而是**这一下点击立刻是四要素错误，队列里一条都没有**。
    assert resp.status_code == 400, resp.text
    err = error_of(resp)
    assert err["code"] == "MISSING_CAPABILITY"
    assert err["title"] == "还没有配置视频生成服务地址"
    assert jobs_of(client, pid) == [], "没绑上之前一条都不许进队列"

    patch_settings(client, {"video.base_url": BASE})
    resp = generate(client, pid, shot_id)
    assert resp.status_code == 201, resp.text
    assert len(jobs_of(client, pid)) == 1, "补上地址，同一次点击就入队了"


def test_the_resolved_route_is_frozen_into_the_job(client: TestClient, pid: str) -> None:
    """入队解析一次并冻结，执行与重试只读冻结值（硬约束 3）。"""
    pause(client, pid)
    shot_id = make_shot(client, pid)
    patch_settings(
        client, {"video.provider": "http_api", "video.base_url": BASE, "video.api_key": SECRET}
    )
    assert generate(client, pid, shot_id).status_code == 201

    job = jobs_of(client, pid)[0]
    frozen = job["params"]["route"]
    assert set(frozen) == {
        "provider",
        "label",
        "source",
        "capability",
        "workflow_id",
        "workflow_name",
        "preset",
        "base_url",
    }, "`ready` / `issues` 说的是解析那一刻缺什么，冻进去会被当成这次任务的失败原因"
    assert (frozen["provider"], frozen["source"]) == ("http_api", "settings")
    assert frozen["capability"] == "image2video"
    assert frozen["base_url"] == BASE, "地址进档：排查时第一个要看的东西"
    assert SECRET not in json.dumps(job["params"], ensure_ascii=False), "密钥永不进档"
    assert job["params"]["generation_mode"] == "http_api", "兼容旧读法的那个键这次是真的"
    assert job["workflow_id"] is None, "绑定那条路才有值：装配条件是「这个任务有绑定的图」"

    # 中途在设置页改回预设那条：**已入队的那一条重试仍然走冻结的那一份**，否则「重试」会
    # 变成「换个后端跑一遍」，而版本参数上写的还是旧那条。
    assert client.post(f"{API}/projects/{pid}/jobs/{job['id']}/cancel").status_code == 200
    patch_settings(client, {"video.provider": "comfy_preset"})
    resp = client.post(f"{API}/projects/{pid}/jobs/{job['id']}/retry")
    assert resp.status_code == 200, resp.text
    again = jobs_of(client, pid)[0]
    assert again["params"]["route"] == frozen, "重试不重新解析"
    assert _provider_of(again["params"]) == "http_api", "执行时读的就是它"
    assert _provider_of({"generation_mode": "comfy_preset"}) == "comfy_preset", "老 job 照旧能跑"

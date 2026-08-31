"""那一句描述怎么一路走到 prompt 里（账单 → `RefAsset` → `ref_hint`）。

「引用一个素材」最终只变成模型看得到的一句话。这条链有三跳，每一跳断了都不会报错——
模型只是照着文件名编一段像样的东西，而那是最难发现的失败。所以这里逐跳钉住：

  1. **账单那条真的带 `desc`**（`services/context.py::_desc_of`），素材自己没写时退回
     实体的设定文字；两边都空时 `desc_missing` 要为真——那句「模型只会看到文件名」的
     提示不该由前端自己算；
  2. **`ref_hint()` 把它渲染进那句说明**，超长在**这一处**截断（`DESC_MAX`），
     且**没有描述时输出与升级前逐字相同**——老工程的 prompt 不该因为多了一列而变样；
  3. **`snapshot()` 冻结 `desc`**：版本轨上要能回答「当次到底喂了哪句话」（硬约束 3）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from app.generation.providers.base import DESC_MAX, RefAsset, clip_desc, ref_hint
from tests.conftest import upload_png

API = "/api/v1"
SENTENCE = "褪色军绿夹克，短发，左颊一道旧疤"


def a_shot_with_a_manual_ref(client: TestClient, pid: str) -> dict[str, Any]:
    """一个镜头 + 一条人工添加的参考素材。人工条目最直接：`desc` 只认 `Asset.description`。"""
    scene = client.post(
        f"{API}/projects/{pid}/scenes", json={"title": "第一场", "prompt": "雨夜"}
    ).json()
    shot = client.post(
        f"{API}/projects/{pid}/scenes/{scene['id']}/shots",
        json={"title": "推近", "prompt": "雨夜，阿岚推门"},
    )
    assert shot.status_code == 201, shot.text
    aid = upload_png(client, pid, "upload", "alan.png")
    ctx = client.post(
        f"{API}/projects/{pid}/shots/{shot.json()['id']}/context/override",
        json={"action": "add", "asset_id": aid, "label": "导演指定的形象参考"},
    )
    assert ctx.status_code == 200, ctx.text
    return {"shot_id": str(shot.json()["id"]), "asset_id": aid}


def manual_item(client: TestClient, pid: str, shot_id: str) -> dict[str, Any]:
    ctx = client.get(f"{API}/projects/{pid}/shots/{shot_id}/context").json()
    return next(i for i in ctx["items"] if i["kind"] == "manual")


def test_the_bill_carries_the_sentence_the_user_typed(client: TestClient, pid: str) -> None:
    made = a_shot_with_a_manual_ref(client, pid)

    bare = manual_item(client, pid, made["shot_id"])
    assert bare["desc"] == "", "还没写描述时这里就该是空的"
    assert bare["desc_missing"] is True, "「模型只会看到文件名」这句判断由后端出"

    client.patch(f"{API}/projects/{pid}/assets/{made['asset_id']}", json={"description": SENTENCE})

    written = manual_item(client, pid, made["shot_id"])
    assert written["desc"] == SENTENCE
    assert written["desc_missing"] is False
    assert written["label"] == "导演指定的形象参考", "`label` 要短，描述不许挤进去"


def test_the_bill_falls_back_to_what_the_entity_says_about_itself(
    client: TestClient, pid: str
) -> None:
    """素材自己没描述时退回实体的设定文字：那比一个文件名好，也是老工程唯一有的东西。"""
    loc = client.post(
        f"{API}/projects/{pid}/locations", json={"name": "城南旧宅", "description": "青砖，苔痕"}
    ).json()
    variant = client.post(
        f"{API}/projects/{pid}/locations/{loc['id']}/variants",
        json={"name": "雨夜", "time_of_day": "夜"},
    ).json()
    client.post(
        f"{API}/projects/{pid}/variants/{variant['id']}/references",
        json={"asset_id": upload_png(client, pid, "location_reference", "loc.png")},
    )
    scene = client.post(
        f"{API}/projects/{pid}/scenes",
        json={"title": "第一场", "prompt": "雨夜", "location_variant_id": variant["id"]},
    ).json()
    shot = client.post(
        f"{API}/projects/{pid}/scenes/{scene['id']}/shots", json={"title": "推近", "prompt": "推门"}
    ).json()

    ctx = client.get(f"{API}/projects/{pid}/shots/{shot['id']}/context").json()
    item = next(i for i in ctx["items"] if i["kind"] == "location_reference")
    assert item["desc"] == "青砖，苔痕", "地点变体没写就退回地点自己那一句"
    assert item["desc_missing"] is False


def test_the_sentence_shows_up_in_the_one_line_the_model_ever_sees() -> None:
    refs = [RefAsset(path=Path("assets/alan.png"), label="阿岚（默认形象）", desc=SENTENCE)]
    hint = ref_hint(refs)
    assert hint == f"参考素材说明：参考图1=阿岚（默认形象）（{SENTENCE}）。"


def test_no_description_renders_exactly_like_before_the_upgrade() -> None:
    """老工程一列都没填，那句说明必须逐字不变——升级不该改写已有工程的 prompt。"""
    refs = [
        RefAsset(path=Path("assets/a.png"), label="阿岚（默认形象）"),
        RefAsset(path=Path("assets/b.png"), label="城南旧宅 · 雨夜"),
    ]
    assert ref_hint(refs) == "参考素材说明：参考图1=阿岚（默认形象）；参考图2=城南旧宅 · 雨夜。"
    # 连 label 都没有时照旧退回文件名
    assert ref_hint([RefAsset(path=Path("assets/裸图.png"))]) == "参考素材说明：参考图1=裸图.png。"


def test_a_long_setting_is_truncated_at_the_only_place_that_truncates() -> None:
    """一段几百字的设定会把 prompt 顶掉。截断规则只有 `providers/base.py` 这一份。"""
    long = "青砖" * 200
    cut = clip_desc(long)
    assert len(cut) == DESC_MAX + 1 and cut.endswith("…")
    assert cut[:-1] in ref_hint([RefAsset(path=Path("a.png"), label="旧宅", desc=long)])
    # 换行压成空格：文本框里敲的回车不该变成 prompt 的结构
    assert clip_desc("第一行\n第二行") == "第一行 第二行"
    assert clip_desc("") == "" and clip_desc(None) == ""  # type: ignore[arg-type]


def test_the_snapshot_freezes_the_sentence_that_was_actually_fed(
    client: TestClient, pid: str
) -> None:
    """当次喂了哪句话要冻结下来（硬约束 3）：之后用户改了描述，旧版本上那句不许跟着变。"""
    made = a_shot_with_a_manual_ref(client, pid)
    client.patch(f"{API}/projects/{pid}/assets/{made['asset_id']}", json={"description": SENTENCE})

    snap = client.get(f"{API}/projects/{pid}/shots/{made['shot_id']}/context/snapshot").json()
    frozen = next(i for i in snap["included"] if i["kind"] == "manual")
    assert frozen["desc"] == SENTENCE

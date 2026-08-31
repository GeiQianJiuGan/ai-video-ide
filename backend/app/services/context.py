"""Context Resolver 与 Context Inspector（Step 6）。

这一层的唯一目标：让「到底喂了什么给模型」变成一张可读的账单。
每一条都必须回答三个问题——哪来的、优先级多少、为什么被包含或被省略。
人可以手动移除 / 添加 / 替换，覆写记录写在 shot.context_overrides_json，
随时可以「恢复自动」。

被采用的条目还带一个 `role`：**哪一张当首帧 / 末帧，剩下的当参考素材**。这条规则只写在这里，
`services/generation.py` 照账单读它，不再自己挑一遍——否则界面上标的和真正喂进去的会分叉。

**首尾帧只认显式指定，绝不提拔参考素材。** 以前这里把优先级最高的那一条（通常是角色表）
自动标成 `first_frame`：界面上给一张三视图标了「首帧」，模型端也真把它当画面第一格用，
于是画面从一张三视图开始。首尾帧决定「画面从哪一格开始 / 结束」，参考素材决定
「谁出场、在哪儿、什么动作、什么声音」——两件事，两处表达。现在首尾帧来自
`Shot.first_frame_asset_id` / `last_frame_asset_id`（用户按下去的那一下，迁移
`0013_shot_frames`），`use_prev_frame` 的镜头才用上游末帧顶首帧（那是 tail_frame 衔接的
全部意义）；两个都没有就是**这个镜头没有首帧**，账单照实说。

**参考素材不只有图。** 每条都带 `media`（`image` / `video` / `audio`，只看后缀，
`assets.kind_of_suffix`），槽位也按媒体分开数（`AIVS_REF_*` / `AIVS_REF_VIDEO_*` /
`AIVS_REF_AUDIO_*`）——三种混在一起数的话，一段 `.mp4` 会被填进 LoadImage，既不报错
也出不了片。认不出来的后缀不采用，理由写在条目上。

**账单不截断。** 「能收几个」不是我们的设置，而是模型端那份图的事实
（`ref_capacity()` 问适配层）。采用的照样全采用，超出槽位的部分变成 `capacity` 块里的
一句警告，生成前要用户确认（`REF_OVER_CAPACITY`）——悄悄少喂两张图，事后没人查得出
人物形象为什么跑偏。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.core.errors import AppError, ErrorCode
from app.generation.providers import presets
from app.generation.providers.base import MEDIA, MEDIA_LABEL, RefCapacity
from app.persistence.models import Project, utc_now
from app.persistence.models_cast import Appearance, Character, SheetVersion
from app.persistence.models_gen import GenerationVersion
from app.persistence.models_story import Scene, SceneCast, SceneLocation, Shot, ShotCast, ShotProp
from app.persistence.models_world import (
    Asset,
    Location,
    LocationReference,
    LocationVariant,
    Prop,
    PropReference,
)
from app.services import params
from app.services.assets import kind_of_suffix
from app.services.base import as_dict, db_of, dump_json, fetch, fetch_all, load_json

#: 优先级：同一角色只留最高的那个形象；上游末帧比道具重要，因为它决定连续性。
#: 显式指定的首 / 末帧排在最前——它们不占参考槽位，但账单上就该在第一行。
PRIORITY = {
    "first_frame": 130,
    "last_frame": 129,
    "manual": 110,
    "character_sheet": 100,
    "location_reference": 90,
    "prev_frame": 80,
    "prop_reference": 60,
}

#: 首帧 / 末帧只能是图片：模型端那两个槽位接的是 LoadImage，喂一段视频进去不出片。
FRAME_KINDS = ("first_frame", "last_frame")

#: 这条素材**是什么**（`MEDIA_LABEL` 说的是它**当什么用**：参考图 / 参考视频 / 参考音频）。
#: 「首帧只能是图片」「不认识这种文件」两句话都用它，别在两处各写一遍。
MEDIA_NOUN = {"image": "图片", "video": "视频", "audio": "音频", "other": "不认识的文件"}


def _asset_label(row: Asset | None, asset_id: str) -> str:
    """账单上显示的文件名。资产行取不到时退回 id——照实说「找不到」比留空好排查。"""
    if row is None:
        return f"资产 {asset_id}（找不到）"
    meta = load_json(row.meta_json, {})
    name = meta.get("filename") if isinstance(meta, dict) else None
    return str(name or Path(row.path).name)


def _desc_of(row: Asset | None, fallback: str = "") -> str:
    """这一条素材「长什么样」——**模型唯一看得到的那句说明**。

    取值顺序只有这一份（各处再取一遍必然分叉）：先认资产自己那句描述
    （`Asset.description`，用户手填或 AI 看图补的），没有就退回这个实体的设定文字
    （角色的外形、地点变体的描述、道具的描述），都没有就是空。

    空不是错误，只是「这一条进 prompt 时只有一个名字」——`desc_missing` 会把它标出来。
    截断不在这里做：那是 `providers/base.py::clip_desc` 的事，账单要留全文给界面看。
    """
    own = str((row.description if row is not None else "") or "").strip()
    return own or " ".join(str(fallback or "").split())


#: 形象的哪几格算「外形描述」。**只有这一处口径**——AI 协作栏的 `list_characters` 靠它
#: 回答「这个形象有没有描述」（`ai/director/tools.py`），判断的必须和真正拼进 prompt 的
#: 是同一批字段，否则模型看到「有描述」而账单里那一条其实是空的。
APPEARANCE_DESC_FIELDS = ("age", "face", "hair", "body", "costume", "traits", "state")


def _appearance_desc(app: Appearance | None, char: Character | None) -> str:
    """形象的外形文字，按 `APPEARANCE_DESC_FIELDS` 的顺序拼一句。

    **只认这个形象自己填的那几格**：继承链的解析在 `services/cast.py::resolve_fields`，
    账单这条只读路径不该为一句 fallback 再走一遍那棵树（真正要紧的形象描述应该写在
    定妆图那张资产的 `description` 上，那才是模型看的那张图）。
    """
    if app is None:
        return str((char.description if char else "") or "").strip()
    parts = [str(getattr(app, f, "") or "").strip() for f in APPEARANCE_DESC_FIELDS]
    joined = "，".join(p for p in parts if p)
    return joined or str((char.description if char else "") or "").strip()



def ref_capacity(capacity: RefCapacity | None = None) -> RefCapacity:
    """模型端这一次能收几张参考图。**不是设置项**——问的是适配层。

    以前这里有个应用级上限 `video.ref_limit`，账单算到第 N 张就把剩下的划掉。那个数字
    与模型端那份图的真实槽位数是两回事，配错一边就白丢用户的角色图 / 场景图，
    还得自己去数 `AIVS_REF_*`。现在账单**不再截断**：采用的照样全采用，
    超出槽位的部分变成生成前的一次警告 + 确认（`REF_OVER_CAPACITY`），
    真正的截断只发生在提交那一刻，并如实写进 `params.ref_notes`。
    没有一份可数的图（通用 REST 合同 / 没选预设）就是不限张数。
    """
    if capacity is not None:
        return capacity
    from app.generation.providers import registry  # 延迟导入：context 不在模块级依赖生成层

    return registry.ref_capacity()


async def project_ref_capacity(pid: str) -> RefCapacity | None:
    """工程选的那份预设是这个工程唯一的参考素材容量来源。

    三种媒体各数一遍（`presets.slot_counts`）：图片槽位是 `AIVS_REF_*`，视频是
    `AIVS_REF_VIDEO_*`，音频是 `AIVS_REF_AUDIO_*`。视频 / 音频 0 槽是常态
    （大多数图只收图），所以只在 detail 里附一句，不当成异常。
    """
    project = (await fetch_all(db_of(pid), Project))[0]
    name = project.r2v_preset_name or project.preset_name
    if not name:
        return None
    counts = presets.slot_counts(name)
    if counts is None:
        return None
    # 列出所有媒体类型的槽位情况
    parts = [f"参考图 {counts['image']} 个"]
    for media in ("video", "audio"):
        parts.append(f"{presets.MEDIA_LABEL[media]} {counts[media]} 个")
    detail = f"当前预设 {name} 支持：{' / '.join(parts)}。"
    return RefCapacity(counts["image"], name, detail, video=counts["video"], audio=counts["audio"])


def _assign_roles(items: list[dict[str, Any]], has_prev: bool) -> None:
    """给采用的条目标上 `role`：`first_frame` / `last_frame`，其余 `reference`。

    规则只有这一份，`services/generation.py` 照它读——以前那边自己又挑了一遍首帧，
    于是检查器上标的和真正喂进去的可能不是同一张。

    **顺序只有两级**：先看镜头上显式指定的那两个槽位（`Shot.first_frame_asset_id` /
    `last_frame_asset_id`，就是用户按下去的那一下），首帧没指定而这个镜头要续接上游时
    才用上游末帧顶上（tail_frame 衔接的全部意义）。**到此为止**——两个都没有就是这个
    镜头没有首帧，绝不把优先级最高的参考素材提拔上来。以前那么做的结果是：界面上给一张
    三视图标了「首帧」，模型端也真把它当画面第一格用，画面从一张三视图开始。
    """
    for item in items:
        item["role"] = "reference" if item.get("included") else ""
    used = [i for i in items if i.get("included")]
    explicit = set()
    for slot in FRAME_KINDS:
        hit = next((i for i in used if i["kind"] == slot), None)
        if hit is not None:
            hit["role"] = slot
            explicit.add(slot)
    if "first_frame" not in explicit and has_prev:
        prev = next((i for i in used if i["kind"] == "prev_frame"), None)
        if prev is not None:
            prev["role"] = "first_frame"


def _capacity_of(items: list[dict[str, Any]], cap: RefCapacity) -> dict[str, Any]:
    """账单这一次会不会有素材喂不进去——**只报，不删**。

    数的是 `role == "reference"` 那几条，而且**按媒体各数一遍**：首 / 末帧走
    `AIVS_FIRST_FRAME` / `AIVS_LAST_FRAME`，不占参考槽位；参考图数 `AIVS_REF_*`、
    参考视频数 `AIVS_REF_VIDEO_*`、参考音频数 `AIVS_REF_AUDIO_*`——三种混在一起数的话，
    图多音频少也会报成「装得下」，然后那段音频被安静地丢掉。
    喂不进去的一定是每一族里**末尾**几条，因为适配器按账单顺序填槽位
    （`comfy_preset._refs` 取前 N 个），优先级最低的先被挤掉。它们照旧 `included=True`，
    只是多一个 `over_capacity` 标记——「这条我采用了、但这份图收不下」和「这条我没采用」
    是两件事，界面上得分得开。

    顶层那几个字段（`limit` / `ref_count` / `dropped` / `dropped_labels`）说的是**参考图**，
    这是历史口径，`generation.drop_entry` 与旧版本里冻结的账单都照它读；三种媒体的完整
    账在 `media` 子块里，`over` 是「任意一种装不下」。

    这是一份估算：真正喂了哪几个由提交那一刻的 `params.refs` / `params.ref_notes` 记录
    （同一份素材重复出现会让数量差一个），出入只会更少不会更多。
    """
    for item in items:
        item["over_capacity"] = False
    refs = [i for i in items if i.get("role") == "reference"]
    per_media: dict[str, dict[str, Any]] = {}
    for media in MEDIA:
        group = [i for i in refs if (i.get("media") or "image") == media]
        dropped = cap.dropped_of(media, len(group))
        tail = group[len(group) - dropped :] if dropped else []
        for item in tail:
            item["over_capacity"] = True
        per_media[media] = {
            "label": MEDIA_LABEL[media],
            #: None = 不限制。0 是有意义的答案（那份图这一族槽位一个都没标）。
            "limit": cap.limit_of(media),
            "ref_count": len(group),
            "dropped": dropped,
            "dropped_labels": [str(i["label"]) for i in tail],
            "over": dropped > 0,
        }
    image = per_media["image"]
    return {
        #: None = 不限制。0 是有意义的答案（那份图一个参考图槽位都没标）。
        "limit": cap.limit,
        "source": cap.source,
        "detail": cap.detail,
        "ref_count": image["ref_count"],
        "dropped": image["dropped"],
        "dropped_labels": image["dropped_labels"],
        "over": any(block["over"] for block in per_media.values()),
        #: 三种媒体各自的账。顶层那几个字段是它的 `image` 那一份（历史口径）。
        "media": per_media,
    }


def _extracted_frame(assets: Any, from_asset_id: str | None, at: str | float = "end") -> str | None:
    """找一找这段视频（这一段区间）的末帧是不是已经抽过了。

    `at` 不只有 `"end"`：长视频切段出来的版本共用同一个源文件，**上游那一段的末帧是
    区间末尾那个时间点**（`frames.tail_at`），拿整段长片的 `"end"` 当它的末帧就会接到
    片尾字幕上去。
    """
    if not from_asset_id:
        return None
    for asset in assets:
        if asset.kind != "frame":
            continue
        meta = load_json(asset.meta_json, {})
        if not isinstance(meta, dict) or meta.get("from_asset_id") != from_asset_id:
            continue
        got = meta.get("at")
        if isinstance(at, str):
            if got == at:
                return str(asset.id)
        elif isinstance(got, (int, float)) and abs(float(got) - float(at)) < 0.001:
            return str(asset.id)
    return None


def _tail_at(version: Any) -> str | float:
    """上游那一版的末帧该抽哪个位置。位置判定只有一份（`frames.tail_at`）。"""
    from app.services.frames import tail_at  # 延迟导入：context 不该在模块级依赖 FFmpeg 层

    if version is None:
        return "end"
    return tail_at(version.out_point, version.in_point)


class ContextService:
    async def resolve(
        self,
        pid: str,
        shot_id: str,
        *,
        capacity_override: RefCapacity | None = None,
        include_prev: bool = True,
    ) -> dict[str, Any]:
        db = db_of(pid)
        shot = await fetch(db, Shot, shot_id, "镜头")
        scene = await fetch(db, Scene, shot.scene_id, "场景")
        overrides: list[dict[str, Any]] = load_json(shot.context_overrides_json, [])
        removed = {o["key"] for o in overrides if o.get("action") == "remove"}
        added = [o for o in overrides if o.get("action") == "add"]

        assets = {a.id: a for a in await fetch_all(db, Asset)}
        items: list[dict[str, Any]] = []
        # 转场跳过角色参考素材，但后面的完整性检查仍需一个稳定的空列表。
        picks: list[str] = []

        # **转场镜头只要首尾帧，不要参考素材。** 两帧之间的过渡只靠首尾帧驱动，
        # 加角色表 / 地点图只会让画面跑偏。
        is_transition = shot.kind == "transition"

        # 0. 镜头上显式指定的首帧 / 末帧：**「哪一张是首帧」是用户按下去的那一下。**
        #    它们不占参考槽位（走 AIVS_FIRST_FRAME / AIVS_LAST_FRAME），但必须上账单——
        #    首尾帧决定画面从哪一格开始 / 结束，是这一次生成里影响最大的两条。
        #    没指定就是没有这一条（老工程两列都是空的，行为与以前一致）。
        for slot, slot_asset_id, slot_label in (
            ("first_frame", shot.first_frame_asset_id, "首帧"),
            ("last_frame", shot.last_frame_asset_id, "末帧"),
        ):
            if not slot_asset_id:
                continue
            row = assets.get(slot_asset_id)
            picked_media = kind_of_suffix(Path(row.path).suffix) if row is not None else "image"
            usable = row is None or picked_media == "image"
            items.append(
                {
                    "key": f"{slot}:{slot_asset_id}",
                    "kind": slot,
                    "label": f"{slot_label} · {_asset_label(row, slot_asset_id)}",
                    "priority": PRIORITY[slot],
                    "asset_id": slot_asset_id,
                    "source_id": None,
                    "eligible": usable,
                    "reason": (
                        f"镜头上指定的{slot_label}"
                        if usable
                        else f"{slot_label}只能是图片，这一个是{MEDIA_NOUN[picked_media]}"
                    ),
                }
            )

        # 转场镜头只要首尾帧，跳过所有参考素材收集（角色表、地点图、道具）。
        if is_transition:
            # 直接跳到最后的采纳与排序环节
            pass
        else:
            # 1. 出场形象的角色表：同一角色保留优先级最高的一个形象。
            #    镜头没单独挂出场表时**继承这一幕的人物**——流程图上那些「人物」小节点
            #    必须真的影响生成，否则只是装饰（幕级清单见 services/story.py::set_scene_cast）。
            cast_rows = await fetch_all(db, ShotCast, where=ShotCast.shot_id == shot_id)
            picks = [r.appearance_id for r in sorted(cast_rows, key=lambda r: r.id)]
            inherited = False
            if not picks:
                scene_cast = await fetch_all(
                    db, SceneCast, where=SceneCast.scene_id == scene.id, order_by=SceneCast.index_no
                )
                picks = [r.appearance_id for r in scene_cast]
                inherited = bool(picks)
            apps = {a.id: a for a in await fetch_all(db, Appearance)}
            chars = {c.id: c for c in await fetch_all(db, Character)}
            sheets = await fetch_all(db, SheetVersion, order_by=SheetVersion.version_no)
            seen_char: set[str] = set()
            for appearance_id in picks:
                app = apps.get(appearance_id)
                if app is None:
                    continue
                char = chars.get(app.character_id)
                mine = [s for s in sheets if s.appearance_id == app.id]
                current = next((s for s in mine if s.is_current), mine[-1] if mine else None)
                label = (
                    f"{char.name if char else '未知角色'}（{app.name}）"
                    f" · Character Sheet v{current.version_no}"
                    if current
                    else f"{char.name if char else '未知角色'}（{app.name}） · 无角色表"
                )
                duplicate = app.character_id in seen_char
                seen_char.add(app.character_id)
                items.append(
                    {
                        "key": f"character_sheet:{app.id}",
                        "kind": "character_sheet",
                        "label": label + ("（本幕人物）" if inherited else ""),
                        "priority": PRIORITY["character_sheet"] if not duplicate else 30,
                        "asset_id": current.asset_id if current else None,
                        "source_id": app.id,
                        "desc_fallback": _appearance_desc(app, char),
                        "eligible": bool(current and current.asset_id) and not duplicate,
                        "reason": (
                            "同一角色已有更高优先级形象"
                            if duplicate
                            else (
                                (
                                    "该角色在本幕出场（镜头没单独挂人物）"
                                    if inherited
                                    else "该角色在本镜头出场"
                                )
                                if current and current.asset_id
                                else "该形象还没有角色表图"
                            )
                        ),
                    }
                )

            # 2. 地点参考：本 Scene 的主地点优先；幕里另外选的地点也能用，只是低一档；
            #    同地点的其他变体列出但省略，理由写清。
            variants = {v.id: v for v in await fetch_all(db, LocationVariant)}
            locations = {loc.id: loc for loc in await fetch_all(db, Location)}
            loc_refs = await fetch_all(db, LocationReference, order_by=LocationReference.created_at)
            chosen = variants.get(scene.location_variant_id or "")
            picked_rows = await fetch_all(
                db,
                SceneLocation,
                where=SceneLocation.scene_id == scene.id,
                order_by=SceneLocation.index_no,
            )
            picked = {r.location_variant_id for r in picked_rows}
            for ref in loc_refs:
                variant = variants.get(ref.variant_id)
                if variant is None:
                    continue
                location = locations.get(variant.location_id)
                same = chosen is not None and variant.id == chosen.id
                extra = not same and variant.id in picked
                sibling = (
                    not same
                    and not extra
                    and chosen is not None
                    and variant.location_id == chosen.location_id
                )
                if not same and not extra and not sibling:
                    continue
                items.append(
                    {
                        "key": f"location_reference:{ref.id}",
                        "kind": "location_reference",
                        "label": f"{location.name if location else '未知地点'} · {variant.name}"
                        + (f" · 机位 {ref.camera}" if ref.camera else ""),
                        "priority": (
                            PRIORITY["location_reference"]
                            if same
                            else (PRIORITY["location_reference"] - 5 if extra else 20)
                        ),
                        "asset_id": ref.asset_id,
                        "source_id": ref.id,
                        "desc_fallback": str(
                            variant.description or (location.description if location else "") or ""
                        ),
                        "eligible": same or extra,
                        "reason": (
                            "本 Scene 选定的地点变体"
                            if same
                            else (
                                "本幕另外选中的地点变体" if extra else "与本 Scene 的时间设定冲突"
                            )
                        ),
                    }
                )

            # 3. 上游镜头末帧：连续性的来源。
            #    注意这里指的是**抽出来的那张图**，不是上游那整段视频——模型端拿一段视频当
            #    首帧是用不了的。抽帧要起 FFmpeg 进程，所以不在这条只读路径上做：还没抽的时候
            #    先标 pending_extract，真正抽取发生在入队前（services/generation.py）。
            if include_prev and shot.prev_shot_id:
                prev = next(
                    (s for s in await fetch_all(db, Shot) if s.id == shot.prev_shot_id), None
                )
                version = None
                if prev is not None and prev.current_version_id:
                    version = next(
                        (
                            v
                            for v in await fetch_all(db, GenerationVersion)
                            if v.id == prev.current_version_id
                        ),
                        None,
                    )
                source_asset = version.asset_id if version else None
                ready = source_asset is not None
                # 上游那一版有区间时（长视频切段），末帧是**区间末尾**那个时间点，
                # 不是整段源文件的结尾——否则一幕里所有镜头都会接到长片的片尾去。
                want_at = _tail_at(version)
                frame = _extracted_frame(assets.values(), source_asset, want_at) if ready else None
                items.append(
                    {
                        "key": f"prev_frame:{shot.prev_shot_id}",
                        "kind": "prev_frame",
                        "label": f"Shot {prev.index_no if prev else '?'} 末帧",
                        "priority": PRIORITY["prev_frame"],
                        "asset_id": frame or source_asset,
                        "source_id": shot.prev_shot_id,
                        #: 它不是素材，是上一段画面的最后一格——「长什么样」由那段视频自己
                        #: 决定，没有可写的描述，所以给一句固定说明而不是留空报缺。
                        "desc_fallback": "上游镜头的真末帧，画面从这一格接着走",
                        "eligible": ready,
                        "pending_extract": bool(ready and frame is None),
                        "from_asset_id": source_asset,
                        #: 抽帧要抽哪个位置（`ensure_frames` 照它抽）。"end" = 整段的结尾。
                        "extract_at": want_at,
                        "reason": (
                            "已抽取的上游末帧，用于保持连续性"
                            if frame
                            else (
                                "上游已出片，生成前会从它抽取末帧"
                                if ready
                                else "上游镜头还没有当前版本，末帧不存在"
                            )
                        ),
                    }
                )

            # 4. 出场道具参考图
            props = {p.id: p for p in await fetch_all(db, Prop)}
            prop_refs = await fetch_all(db, PropReference)
            for row in await fetch_all(db, ShotProp, where=ShotProp.shot_id == shot_id):
                prop = props.get(row.prop_id)
                mine = [r for r in prop_refs if r.prop_id == row.prop_id]
                current = next((r for r in mine if r.is_current), None)
                present = row.state == "present"
                items.append(
                    {
                        "key": f"prop_reference:{row.prop_id}",
                        "kind": "prop_reference",
                        "label": f"{prop.name if prop else '未知道具'} · 参考图 v{current.version_no}"
                        if current
                        else f"{prop.name if prop else '未知道具'} · 无参考图",
                        "priority": PRIORITY["prop_reference"] if present else 10,
                        "asset_id": current.asset_id if current else None,
                        "source_id": row.prop_id,
                        "desc_fallback": str((prop.description if prop else "") or ""),
                        "eligible": bool(current) and present,
                        "reason": ("本镜头出场道具" if present else "该道具在本镜头标为已丢弃")
                        if current
                        else "该道具还没有参考图",
                    }
                )

            # 5. 人工添加的条目：优先级最高，永不被自动逻辑挤掉
            for extra in added:
                items.append(
                    {
                        "key": extra.get("key") or f"manual:{extra.get('asset_id')}",
                        "kind": "manual",
                        "label": extra.get("label") or "手动添加的参考素材",
                        "priority": PRIORITY["manual"],
                        "asset_id": extra.get("asset_id"),
                        "source_id": None,
                        "eligible": True,
                        "reason": "手动添加",
                    }
                )

        # 排序 → 应用覆写（**不再卡上限**：能收几个是模型端的事，超了在生成前问用户）
        items.sort(key=lambda i: (-int(i["priority"]), str(i["key"])))
        included = 0
        for item in items:
            asset = assets.get(item["asset_id"] or "")
            #: 这一条是什么媒体：**只看后缀**（`assets.kind_of_suffix`）。槽位按媒体分开数，
            #: 所以这个字段决定它去数哪一族（`AIVS_REF_*` / `AIVS_REF_VIDEO_*` /
            #: `AIVS_REF_AUDIO_*`）。资产行取不到时按图片算——那是 `missing_file`，
            #: 「媒体未知」不等于「这种媒体喂不进去」。
            media = kind_of_suffix(Path(asset.path).suffix) if asset is not None else "image"
            item["media"] = media
            #: 这一条进 prompt 时那句「长什么样」。**只在这一处取一次**（`_desc_of`）：
            #: 先认资产自己的描述，没有就退回构造时留下的实体设定（`desc_fallback`）。
            #: 账单里存全文，截断由 `providers/base.py::clip_desc` 在提交那一刻做。
            item["desc"] = _desc_of(asset, str(item.pop("desc_fallback", "") or ""))
            #: 空描述不是错误，但要显眼：没有它，模型引用这张素材时只看到一个文件名。
            #: 这句判断由后端给，不让两个前端渲染处各算一遍。
            item["desc_missing"] = not item["desc"]
            item["manual"] = item["kind"] == "manual" or item["key"] in removed
            if item["key"] in removed:
                item["included"] = False
                item["reason"] = "手动移除"
            elif not item["eligible"]:
                item["included"] = False
            elif item["asset_id"] is None:
                item["included"] = False
                item["reason"] = "没有可用的资产"
            elif media == "other":
                # 认不出后缀的一律不采用：当图填进 LoadImage 既不报错也出不了片，
                # 悄悄喂进去比明说更糟。
                item["included"] = False
                item["reason"] = (
                    f"不认识这种文件（{Path(asset.path).suffix or '没有后缀'}），"
                    "参考素材只能是图片 / 视频 / 音频"
                )
            else:
                item["included"] = True
                included += 1
            item["asset_path"] = asset.path if asset else None
            item["missing_file"] = bool(item["asset_id"]) and asset is None
            item.pop("eligible", None)
        _assign_roles(items, bool(include_prev and shot.prev_shot_id))
        selected_capacity = capacity_override or await project_ref_capacity(pid)
        capacity = _capacity_of(items, ref_capacity(selected_capacity))

        problems = []
        # **要什么由这一幕的来源决定**（`params.SCENE_REQUIRED`）：导入幕的画面已经有了，
        # 再要求它选地点、挑角色、写 prompt 只是三道消不掉的门槛。
        if params.requires(scene, "location") and not scene.location_variant_id:
            problems.append("本 Scene 还没有选定地点变体")
        if params.requires(scene, "cast") and not picks:
            problems.append("本镜头没有出场角色")
        # 取值口径只有一份（`services/params.py::prompt_of`）。
        if params.prompt_missing(shot, scene):
            problems.append("既没有 prompt 也没有画面描述")
        if (
            include_prev
            and shot.prev_shot_id
            and not any(i["kind"] == "prev_frame" and i["included"] for i in items)
        ):
            problems.append("需要上游末帧，但上游镜头还没有当前版本")

        return {
            "shot_id": shot_id,
            "items": items,
            "included_count": included,
            #: 「模型端能收几张、这次会不会有图喂不进去」。以前这里是应用级上限 `limit` /
            #: `at_limit`，现在换成这一整块——上限不再是我们配的数字。
            "capacity": capacity,
            "complete": not problems,
            "problems": problems,
            "overrides": overrides,
            "resolved_at": utc_now(),
        }

    async def ensure_frames(
        self, pid: str, shot_id: str, *, include_prev: bool = True
    ) -> dict[str, Any]:
        """把「生成前会抽取」那些条目真的抽出来，然后重新出账单。

        单独一个方法而不是塞进 `resolve`：resolve 是只读的、UI 会频繁调，
        起 FFmpeg 进程不该发生在那条路径上。入队前调这个。
        """
        ctx = await self.resolve(pid, shot_id, include_prev=include_prev)
        pending = [i for i in ctx["items"] if i.get("pending_extract") and i.get("from_asset_id")]
        if not pending:
            return ctx
        from app.services.frames import frames  # 延迟导入：context 不该在模块级依赖 FFmpeg 层

        for item in pending:
            at = item.get("extract_at")
            await frames.extract(pid, str(item["from_asset_id"]), "end" if at is None else at)
        return await self.resolve(pid, shot_id, include_prev=include_prev)

    async def require_complete(
        self, pid: str, shot_id: str, *, include_prev: bool = True
    ) -> dict[str, Any]:
        """生成前的门槛：上下文不完整就明确拒绝，而不是生成一张废图。"""
        ctx = await self.ensure_frames(pid, shot_id, include_prev=include_prev)
        if not ctx["complete"]:
            raise AppError(
                ErrorCode.CONTEXT_INCOMPLETE,
                "上下文不完整",
                "；".join(ctx["problems"]),
                [
                    "在镜头编辑器的上下文检查器里补齐缺失项",
                    "或先给本 Scene 选一个地点变体",
                    "确认无误也可以在检查器里手动添加参考图",
                ],
                {"shot_id": shot_id},
            )
        return ctx

    # --- 人工干预 ---

    async def override(
        self, pid: str, shot_id: str, action: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """action ∈ remove / add / reset。replace = remove 旧的 + add 新的。"""
        if action not in ("remove", "add", "reset"):
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "未知的干预动作",
                f"{action} 不在 remove / add / reset 里。",
                ["用「移除」「添加」或「恢复自动」"],
            )
        db = db_of(pid)
        shot = await fetch(db, Shot, shot_id, "镜头")
        overrides: list[dict[str, Any]] = load_json(shot.context_overrides_json, [])

        if action == "reset":
            key = payload.get("key")
            overrides = [] if not key else [o for o in overrides if o.get("key") != key]
        elif action == "remove":
            key = str(payload.get("key") or "")
            if not key:
                raise AppError(
                    ErrorCode.VALIDATION_ERROR,
                    "缺少要移除的条目",
                    "payload.key 是必填的。",
                    ["从上下文清单里点某一行的「移除」"],
                )
            overrides = [o for o in overrides if o.get("key") != key]
            overrides.append({"action": "remove", "key": key, "at": utc_now()})
        else:
            asset_id = str(payload.get("asset_id") or "")
            if not asset_id:
                raise AppError(
                    ErrorCode.VALIDATION_ERROR,
                    "缺少要添加的资产",
                    "payload.asset_id 是必填的。",
                    ["从资产库里选一张图再添加"],
                )
            await fetch(db, Asset, asset_id, "资产")
            overrides.append(
                {
                    "action": "add",
                    "key": f"manual:{asset_id}",
                    "asset_id": asset_id,
                    "label": payload.get("label"),
                    "at": utc_now(),
                }
            )

        async with db.write() as session:
            row = await session.get(Shot, shot_id)
            assert row is not None
            row.context_overrides_json = dump_json(overrides)
            row.updated_at = utc_now()
        return await self.resolve(pid, shot_id)

    async def snapshot(
        self, pid: str, shot_id: str, *, include_prev: bool = True
    ) -> dict[str, Any]:
        """冻结进 GenerationVersion.context_json 的那份账单。"""
        ctx = await self.resolve(pid, shot_id, include_prev=include_prev)
        return {
            "resolved_at": ctx["resolved_at"],
            #: 当时模型端能收几张、这次算出要丢几张。冻结它，事后才说得清
            #: 「为什么少喂了两张」——真正喂了哪几张在 `params.refs` / `params.ref_notes`。
            "capacity": ctx["capacity"],
            "included": [
                {
                    "key": i["key"],
                    "kind": i["kind"],
                    "label": i["label"],
                    "asset_id": i["asset_id"],
                    "priority": i["priority"],
                    #: 这一条是当首帧 / 末帧还是当参考素材。冻结它，事后才说得清「喂了什么」。
                    "role": i.get("role") or "reference",
                    #: 是图 / 是视频 / 是音频。槽位按媒体分开算，所以这个也得冻结——
                    #: 不然事后看不出「那段音频到底有没有送出去」。
                    "media": i.get("media") or "image",
                    #: 那句「长什么样」——**模型唯一看得到的素材说明**。冻结它，事后才说得清
                    #: 「当时到底喂了哪句话」（描述后来被改过，版本里这一份不变）。
                    "desc": i.get("desc") or "",
                    #: 采用了、但这份图收不下（会在提交时按顺序被挤掉）。
                    "over_capacity": bool(i.get("over_capacity")),
                }
                for i in ctx["items"]
                if i["included"]
            ],
            "omitted": [
                {"key": i["key"], "label": i["label"], "reason": i["reason"]}
                for i in ctx["items"]
                if not i["included"]
            ],
        }

    async def asset_of(self, pid: str, asset_id: str) -> dict[str, Any]:
        return as_dict(await fetch(db_of(pid), Asset, asset_id, "资产"))


context = ContextService()

"""模型无关的「导演意图」——创作层与规范层之间那份唯一的中间形状。

AI 导演产的是**这一份意图**（`beat` / `action` / `shot_size` / … 这些人和任何模型都读得懂的
东西），而不是某个模型认得的 prompt。**「意图 → 某个模型的 prompt」的翻译全在规范层**
（`app/generation/renderers.py`）：同一份意图配不同的 `route.skill` 就渲染成不同模型的形状，
这正是把硬约束 1「业务层不绑具体视频模型」延伸到 prompt 层——导演不该知道下游是不是 MiniMax H3。

**这份 schema 只有这一处**，理由和账单里的取值口径一样：意图字段散在 ai / service / generation
三层里各写一份，迟早对不上（这边叫 `shot_size`、那边叫 `camera`，渲染时就丢一段）。所以：

  - `properties()`——给 LLM 工具用的 `intent` 对象字段表（`ai/director/tools.py` 直接铺进
    `add_shot` / `update_shot` / `add_scene` 的参数里）；
  - `coerce(raw)` / `merge(before, given)`——把模型给的（可能缺字段、类型不齐）归一成干净 dict，
    `merge` 走「给了才覆盖、没给保留原样」（与 `_clean` 同一条口径，改一段不擦掉别的）；
  - `to_segments(intent)`——**唯一的意图 → 三段桥**（`visual_prompt` / `camera_motion` /
    `audio_dialogue`），规范层的渲染器和 service 层落库时的预览 prompt 都过这一座桥，
    两处各拼一遍必然分叉；
  - `missing(intent)`——这份意图缺了哪几样最要命的（Phase 3 的体检器在此基础上再加跨镜连贯）。

**无配乐是恒定约束，不是模型能改的字段**：`no_scoring_music` 由 `coerce` 恒置 `True`，
`properties()` 里根本不列它——一线模型都在抛弃负向，无配乐这条硬约束只留正向一处口径
（渲染层的 `non_diegetic_music: none`），模型不该、也不能把它写成别的。
"""

from __future__ import annotations

from typing import Any

#: 意图的全部字段（顺序即工具参数表与落库 dict 的展示顺序）。`no_scoring_music` 是恒定标记，
#: 不进 `properties()`（模型不写），但进 `coerce` 的输出（恒 True）。
TEXT_FIELDS = (
    "beat",
    "action",
    "shot_size",
    "angle",
    "movement",
    "first_frame",
    "last_frame",
    "dialogue",
    "mood",
)


def properties() -> dict[str, dict[str, Any]]:
    """给 LLM 工具用的 `intent` 对象字段表。**模型面向的字段只有这些**（不含 `no_scoring_music`）。"""
    return {
        "beat": {"type": "string", "description": "这一镜的剧情核心：此刻在讲什么、推进到哪一步"},
        "action": {"type": "string", "description": "画面里主体的具体动作与走位（谁在做什么）"},
        "shot_size": {"type": "string", "description": "景别：远景 / 全景 / 中景 / 近景 / 特写"},
        "angle": {"type": "string", "description": "机位视角：平视 / 俯视 / 仰视 / 过肩 等"},
        "movement": {"type": "string", "description": "运镜：固定 / 推 / 拉 / 摇 / 移 / 跟"},
        "first_frame": {"type": "string", "description": "起始画面（首帧）的构图与内容"},
        "last_frame": {"type": "string", "description": "末帧定格的构图与内容，可留空"},
        "dialogue": {
            "type": "string",
            "description": "对白原文 + 同期环境声 / 动作音效；没有对白就只写声音设计，别编台词",
        },
        "subjects": {
            "type": "array",
            "items": {"type": "string"},
            "description": "这一镜出场的主体（人物 / 关键道具），用剧本里的名字原文",
        },
        "duration": {
            "type": "number",
            "description": "画面时长（秒），约 4~15：空镜可短、情绪戏可长",
        },
        "mood": {"type": "string", "description": "整体氛围 / 影调，可留空"},
    }


def coerce(raw: Any) -> dict[str, Any]:
    """把模型给的意图（可能缺字段、类型不齐、带空串）归一成干净 dict。

    **只留有内容的字段**（空串 / 空列表按「没给」丢掉，与 `_clean` 同一条口径），
    但 `duration` 给了数字就留、`no_scoring_music` 恒置 `True`——那是硬约束，不是可选项。
    """
    src = raw if isinstance(raw, dict) else {}
    out: dict[str, Any] = {}
    for field in TEXT_FIELDS:
        val = str(src.get(field) or "").strip()
        if val:
            out[field] = val
    subjects = [str(s).strip() for s in (src.get("subjects") or []) if str(s).strip()]
    if subjects:
        # 去重、保序：模型偶尔会把同一个人写两遍。
        out["subjects"] = list(dict.fromkeys(subjects))
    dur = src.get("duration")
    if dur is not None:
        try:
            out["duration"] = max(1.0, min(60.0, float(dur)))
        except (TypeError, ValueError):
            pass
    out["no_scoring_music"] = True
    return out


def merge(before: Any, given: Any) -> tuple[dict[str, Any], list[str]]:
    """把这次给的意图叠到原有意图上。**给了才覆盖，没给保留原样**（与 `_clean` 同口径）。

    改一个镜头时模型往往只给 `shot_size` 一项，直接用新意图会把原来的剧情、对白擦成空——
    所以先 `coerce(before)` 打底，再用 `coerce(given)`（已经把空字段剪掉）覆盖上去。
    """
    warnings: list[str] = []
    if given is not None and not isinstance(given, dict):
        warnings.append("intent 不是一个对象，这次的 intent 已忽略")
        given = None
    out = coerce(before)
    out.update(coerce(given))
    out["no_scoring_music"] = True
    return out, warnings


def to_segments(intent: Any) -> dict[str, str]:
    """**唯一的意图 → 三段桥**：模型无关的意图翻成 `visual_prompt` / `camera_motion` /
    `audio_dialogue`。规范层渲染器（`renderers.render_prompt`）与 service 层落库时的预览
    prompt 都过这一座桥，两处各拼一遍必然分叉。

    画面那一段把「首帧 → 剧情 → 动作 → 末帧 → 氛围」顺成一句可读的中文；**保留「首帧」/「末帧」
    这两个词**是有意的——H3 渲染层（`base._detail`）会把它们替换成 `首帧（<Picture n>）`，
    做画面与真实首尾帧的硬绑。机位那一段是「景别，视角，运镜」，声音那一段就是对白 / 声音设计。
    """
    it = coerce(intent)
    bits: list[str] = []
    if it.get("first_frame"):
        bits.append(f"首帧定格于{it['first_frame']}")
    if it.get("beat"):
        bits.append(it["beat"])
    if it.get("action"):
        bits.append(it["action"])
    if it.get("last_frame"):
        bits.append(f"末帧定格于{it['last_frame']}")
    if it.get("mood"):
        bits.append(f"整体氛围：{it['mood']}")
    visual = "，".join(b.strip("，。；;、 ") for b in bits if b).strip()
    if visual and not visual.endswith(("。", "！", "？", ".", "!", "?")):
        visual += "。"
    camera = "，".join(x for x in (it.get("shot_size"), it.get("angle"), it.get("movement")) if x)
    return {
        "visual_prompt": visual,
        "camera_motion": camera,
        "audio_dialogue": str(it.get("dialogue") or ""),
    }


def missing(intent: Any) -> list[str]:
    """这份意图缺了哪几样最要命的（字段名）。Phase 3 的体检器在此之上再加跨镜连贯等判断。

    只盯「缺了它这一镜就渲染不出画面」的那几样：剧情、动作、景别、起始画面。对白允许没有
    （空镜、纯环境声），`last_frame` / `mood` 本就可空，所以都不算缺。
    """
    it = coerce(intent)
    gaps: list[str] = []
    for field in ("beat", "action", "shot_size", "first_frame"):
        if not it.get(field):
            gaps.append(field)
    return gaps

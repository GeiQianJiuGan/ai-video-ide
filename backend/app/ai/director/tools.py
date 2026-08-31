"""AI 导演的工具箱。

工具分成两类，这条界线是整个 agent 的安全边界：

  - **读工具**（`list_*` / `get_scene` / `read_script` / `read_skill`）**立刻执行**。
    它们没有副作用，模型需要先看清这个工程里已经有谁、有哪些地点、现在几幕、
    剧本原文写了什么，才可能提出像样的建议。
  - **写工具**（`add_scene` / `add_shot` / `set_link` / …）**永远不落库**，只被翻译成一条
    **提案**塞进缓冲区。数据库是用户的，改它必须经过用户逐条点头（照
    `story.propose_breakdown` / `apply_breakdown` 的老规矩）。

提案条目的形状固定为 `{op, target, temp_id, before, after, why, warnings}`：

  - `op` 就是写工具名；用户在界面上丢弃一条时，前端把它改成 `"reject"`，
    `services/director.py::apply` 只落 `op != "reject"` 的条目；
  - `before` 是现在库里长什么样（update / delete 才有），`after` 是提案要改成什么——
    有这两半，前端才能画出真正的 Diff 而不是「模型说它要改点东西」；
  - `warnings` 是「这条能落，但有点不对」（比如角色名对不上任何角色）。
    对不上就写出来，绝不静默丢掉。

**`read_script` 是「一次性拆解会超时」那个毛病的解药**：模型自己按 offset 分段读原文，
一段一段地提案，于是每一轮 chat 的输入输出都是有界的——不再需要一次吐出整部片子。
"""

from __future__ import annotations

from typing import Any

from app.ai import prompts, skills
from app.core.errors import AppError, ErrorCode
from app.generation.providers import registry
from app.generation.providers.base import DESC_MAX
from app.persistence.models_flow import LINK_MODES, SHOT_LINK_MODES
from app.persistence.models_gen import IMAGE_TARGETS
from app.services.assets import assets
from app.services.cast import cast
from app.services.context import APPEARANCE_DESC_FIELDS
from app.services.describe import DESC_TARGET_LABEL, DESC_TARGETS, describe
from app.services.images import images
from app.services.sequence import sequence
from app.services.story import story
from app.services.world import world

#: `read_script` 一次最多给多少字。再大就等于把整部剧本塞回上下文，那正是要治的病。
SCRIPT_CHUNK = 2000
SCRIPT_CHUNK_MAX = 6000

#: 镜头 prompt 的三段 + 负向 + 照的哪份 SKILL。**只有这一处口径**：`add_shot` /
#: `update_shot` / `add_scene` 里的 `shots[]` 收的都是这几个字段，正向那段完整 prompt
#: 由 `prompts.format_shot_prompt()` 拼、再过 `prompts.with_shot_audio_policy()`。
SHOT_PROMPT_PARAMS: dict[str, dict[str, Any]] = {
    "camera_motion": {"type": "string", "description": "机位、景别与运镜，如「中景，缓慢推进」"},
    "visual_prompt": {"type": "string", "description": "只写画面里看得见的东西 + SKILL 的锚定语"},
    "audio_dialogue": {"type": "string", "description": "同期环境声、动作音效与对白原文"},
    "negative_prompt": {"type": "string", "description": "逗号分隔的模型规避项"},
    "skill": {
        "type": "string",
        "enum": list(skills.NAMES),
        "description": "照的是哪一份内置 SKILL（先用 read_skill 取全文）",
    },
}

#: 素材图那两个字段。**只有这一处口径**：`add_character` / `add_location` / `add_prop` /
#: `generate_reference` 收的都是这两个，正 / 负向 prompt 由
#: `skills.render_image_prompt()` 在 `to_op()` 里拼一次。
IMAGE_PROMPT_PARAMS: dict[str, dict[str, Any]] = {
    "image_prompt": {
        "type": "string",
        "description": (
            "**只写「长什么样」**：外形、年龄气质、服装配色、材质、时间与天气。"
            "四视图、纯背景、无文字、场景里无人物那些话由系统补，不要自己写"
        ),
    },
    "skill": {
        "type": "string",
        "enum": list(skills.IMAGE_NAMES),
        "description": "照的是哪一份出图 SKILL（留空按素材类型自动选；全文用 read_skill 取）",
    },
}

#: 镜头上那些「不是 prompt」的字段。
SHOT_PLAIN_PARAMS: dict[str, dict[str, Any]] = {
    "title": {"type": "string", "description": "一句话概括这一镜在讲什么"},
    "description": {"type": "string", "description": "这一镜的画面描述（人也要看的那份）"},
    "duration": {"type": "number", "description": "秒，2~8：空镜短、情绪戏长"},
    "camera": {"type": "string", "description": "景别：远景 / 全景 / 中景 / 近景 / 特写"},
    "movement": {"type": "string", "description": "运镜：固定 / 推 / 拉 / 摇 / 跟"},
    "character_names": {
        "type": "array",
        "items": {"type": "string"},
        "description": "这一镜的出场角色，用剧本里的人名原文",
    },
}

#: 工具白名单。模型只能调这里面的东西——名字不在这张表里直接报错，
#: 不去猜「它大概是想调 add_scene」。
TOOLS: dict[str, dict[str, Any]] = {
    "list_characters": {
        "kind": "read",
        "desc": (
            "列出工程里所有角色及其形象（形象 id 是给镜头挂人用的）。"
            "每条带 description 与形象的 has_description——空的那些进 prompt 时只有一个名字。"
        ),
        "params": {},
    },
    "list_locations": {
        "kind": "read",
        "desc": (
            "列出所有地点及其变体（白天 / 雨夜等），变体 id 用来钉住一幕的地点。"
            "变体带 description（它会被拼进引用这个地点的镜头的 prompt）。"
        ),
        "params": {},
    },
    "list_props": {"kind": "read", "desc": "列出所有道具（带 description）。", "params": {}},
    "list_undescribed": {
        "kind": "read",
        "desc": (
            "列出**还没有描述**的素材。素材被镜头引用时，模型看到的只有那一句描述——"
            "空的等于只递过去一个文件名，人物形象在几秒里就丢了。"
            "抽出来的首尾帧是临时文件，不在这张清单里。"
        ),
        "params": {},
    },
    "look_at_image": {
        "kind": "read",
        "desc": (
            "看一张素材图，回一句「它长什么样」的建议文字。**只是建议，一行库都不改**——"
            "要落库请再用 set_description 提一条案。非图片素材与看不了图的端会说明原因。"
        ),
        "params": {"asset_id": {"type": "string", "description": "资产 id"}},
        "required": ["asset_id"],
    },
    "list_scenes": {
        "kind": "read",
        "desc": "列出现有的幕（含顺序、地点、镜头数）与幕之间的衔接方式。",
        "params": {},
    },
    "get_scene": {
        "kind": "read",
        "desc": "看某一幕的细节：它的镜头清单、每个镜头的时长与出场角色。",
        "params": {"scene_id": {"type": "string", "description": "幕 id"}},
        "required": ["scene_id"],
    },
    "read_script": {
        "kind": "read",
        "desc": (
            "读剧本原文的一段。一次只给一段，返回里的 next_offset / done 告诉你还剩多少——"
            "拆长剧本时一段一段读、一段一段提案，不要想一次读完。"
        ),
        "params": {
            "offset": {"type": "integer", "description": "从第几个字开始，第一次给 0"},
            "limit": {
                "type": "integer",
                "description": f"这一段最多多少字，默认 {SCRIPT_CHUNK}，上限 {SCRIPT_CHUNK_MAX}",
            },
        },
    },
    "read_skill": {
        "kind": "read",
        "desc": (
            "取一份内置 SKILL 的全文。**两族共用这一个工具**——写镜头 prompt 之前读对应的"
            "那一份，出素材参考图之前读出图那一份：\n"
            + skills.catalog()
            + "\n"
            + skills.image_catalog()
        ),
        "params": {
            "name": {
                "type": "string",
                "enum": list(skills.ALL_NAMES),
                "description": "SKILL 名",
            }
        },
        "required": ["name"],
    },
    "add_scene": {
        "kind": "write",
        "desc": "提议加一幕。可以顺带给出这一幕的镜头清单。",
        "params": {
            "title": {"type": "string", "description": "这一幕的标题"},
            "summary": {"type": "string", "description": "一两句剧情概要"},
            "time_of_day": {"type": "string", "description": "白天 / 黄昏 / 雨夜等"},
            "location_variant_id": {"type": "string", "description": "地点变体 id，可留空"},
            "shots": {
                "type": "array",
                "description": "这一幕的镜头，按时间顺序",
                "items": {
                    "type": "object",
                    "properties": {**SHOT_PLAIN_PARAMS, **SHOT_PROMPT_PARAMS},
                },
            },
            "why": {"type": "string", "description": "为什么要加这一幕"},
        },
        "required": ["title"],
    },
    "update_scene": {
        "kind": "write",
        "desc": "提议改一幕的标题 / 概要 / 时间 / 地点变体。",
        "params": {
            "scene_id": {"type": "string"},
            "title": {"type": "string"},
            "summary": {"type": "string"},
            "time_of_day": {"type": "string"},
            "location_variant_id": {"type": "string"},
            "why": {"type": "string"},
        },
        "required": ["scene_id"],
    },
    "set_scene_prompt": {
        "kind": "write",
        "desc": "提议改这一幕全部镜头的画面描述（prompt）。",
        "params": {
            "scene_id": {"type": "string"},
            "prompt": {"type": "string", "description": "喂给视频模型的画面描述"},
            "why": {"type": "string"},
        },
        "required": ["scene_id", "prompt"],
    },
    "set_scene_cast": {
        "kind": "write",
        "desc": "提议把一批角色设为这一幕全部镜头的出场角色（整幕覆盖，不是追加）。",
        "params": {
            "scene_id": {"type": "string"},
            "character_names": {
                "type": "array",
                "items": {"type": "string"},
                "description": "角色名；用默认形象。也可以直接给 appearance_ids",
            },
            "appearance_ids": {"type": "array", "items": {"type": "string"}},
            "why": {"type": "string"},
        },
        "required": ["scene_id"],
    },
    "set_scene_props": {
        "kind": "write",
        "desc": "提议把一批道具设为这一幕全部镜头出现的道具（整幕覆盖）。",
        "params": {
            "scene_id": {"type": "string"},
            "prop_names": {"type": "array", "items": {"type": "string"}},
            "prop_ids": {"type": "array", "items": {"type": "string"}},
            "why": {"type": "string"},
        },
        "required": ["scene_id"],
    },
    "set_link": {
        "kind": "write",
        "desc": (
            "提议两幕之间怎么接：cut 硬切 / transition 生成 1~2 秒转场 / "
            "tail_frame 上一幕真末帧当下一幕首帧。"
        ),
        "params": {
            "from_scene_id": {"type": "string"},
            "to_scene_id": {"type": "string"},
            "mode": {"type": "string", "enum": list(LINK_MODES)},
            "duration": {"type": "number", "description": "转场秒数，0.5~4，只有 transition 用"},
            "prompt": {"type": "string", "description": "转场的画面描述"},
            "why": {"type": "string"},
        },
        "required": ["from_scene_id", "to_scene_id", "mode"],
    },
    "reorder_scenes": {
        "kind": "write",
        "desc": "提议重排幕的顺序。order 要给出全部幕 id。",
        "params": {
            "order": {"type": "array", "items": {"type": "string"}},
            "why": {"type": "string"},
        },
        "required": ["order"],
    },
    "delete_scene": {
        "kind": "write",
        "desc": "提议删掉一幕（它的镜头与版本会一起没，所以 why 要写清楚）。",
        "params": {"scene_id": {"type": "string"}, "why": {"type": "string"}},
        "required": ["scene_id"],
    },
    "add_shot": {
        "kind": "write",
        "desc": "提议往某一幕里加一个镜头。prompt 分四段给，别自己拼整段。",
        "params": {
            "scene_id": {"type": "string", "description": "加到哪一幕"},
            "position": {
                "type": "integer",
                "description": "插在这一幕的第几个（1 起）。不给就排在最后",
            },
            **SHOT_PLAIN_PARAMS,
            **SHOT_PROMPT_PARAMS,
            "why": {"type": "string"},
        },
        "required": ["scene_id"],
    },
    "update_shot": {
        "kind": "write",
        "desc": (
            "提议改一个镜头。只给要改的字段；给了 prompt 三段里的任意一段就会重拼那段"
            "完整 prompt，没给的那几段保留原样。"
        ),
        "params": {
            "shot_id": {"type": "string"},
            **SHOT_PLAIN_PARAMS,
            **SHOT_PROMPT_PARAMS,
            "why": {"type": "string"},
        },
        "required": ["shot_id"],
    },
    "delete_shot": {
        "kind": "write",
        "desc": "提议删掉一个镜头（它生成过的版本会一起没）。",
        "params": {"shot_id": {"type": "string"}, "why": {"type": "string"}},
        "required": ["shot_id"],
    },
    "reorder_shots": {
        "kind": "write",
        "desc": "提议重排一幕里镜头的顺序。order 要给出这一幕全部镜头 id。",
        "params": {
            "scene_id": {"type": "string"},
            "order": {"type": "array", "items": {"type": "string"}},
            "why": {"type": "string"},
        },
        "required": ["scene_id", "order"],
    },
    "set_shot_link": {
        "kind": "write",
        "desc": (
            "提议同一幕内相邻两镜之间怎么接：cut 直接切 / transition 补一段短转场。"
            "「续接上游末帧」不在这里——那是镜头的上游依赖（prev_shot_id）。"
        ),
        "params": {
            "from_shot_id": {"type": "string"},
            "to_shot_id": {"type": "string"},
            "mode": {"type": "string", "enum": list(SHOT_LINK_MODES)},
            "duration": {"type": "number", "description": "转场秒数，只有 transition 用"},
            "prompt": {"type": "string", "description": "转场的画面描述"},
            "why": {"type": "string"},
        },
        "required": ["from_shot_id", "to_shot_id", "mode"],
    },
    "add_character": {
        "kind": "write",
        "desc": (
            "提议加一个角色（会顺手建一个「默认形象」）。给了 image_prompt 就顺带排一张"
            "角色四视图参考图；图片服务没配置时只建角色，并把原因写进 warnings。"
        ),
        "params": {
            "name": {"type": "string", "description": "角色名，用剧本里的原文"},
            "description": {"type": "string", "description": "人物设定（给人看的那份）"},
            **IMAGE_PROMPT_PARAMS,
            "why": {"type": "string"},
        },
        "required": ["name"],
    },
    "add_location": {
        "kind": "write",
        "desc": (
            "提议加一个地点（会顺手建一个变体，幕靠变体钉住地点）。给了 image_prompt 就顺带"
            "排一张场景参考图。"
        ),
        "params": {
            "name": {"type": "string", "description": "地点名，如「城南旧宅」"},
            "variant": {"type": "string", "description": "变体名，如「雨夜」；留空叫「默认场景」"},
            "time_of_day": {"type": "string", "description": "白天 / 黄昏 / 雨夜等"},
            "description": {"type": "string", "description": "地点设定（给人看的那份）"},
            **IMAGE_PROMPT_PARAMS,
            "why": {"type": "string"},
        },
        "required": ["name"],
    },
    "add_prop": {
        "kind": "write",
        "desc": "提议加一个道具。给了 image_prompt 就顺带排一张道具参考图。",
        "params": {
            "name": {"type": "string", "description": "道具名，如「油纸伞」"},
            "description": {"type": "string", "description": "道具设定（给人看的那份）"},
            **IMAGE_PROMPT_PARAMS,
            "why": {"type": "string"},
        },
        "required": ["name"],
    },
    "generate_reference": {
        "kind": "write",
        "desc": (
            "提议给**已有**素材补一张参考图（形象 / 地点变体 / 道具），或给一个镜头出一张"
            "首 / 末帧候选。镜头那两种**只进素材库**，设不设成首帧由用户自己点。"
        ),
        "params": {
            "target_kind": {
                "type": "string",
                "enum": list(IMAGE_TARGETS),
                "description": "给哪种素材出图",
            },
            "target_id": {
                "type": "string",
                "description": "那一行的 id：形象 id / 地点变体 id / 道具 id / 镜头 id",
            },
            **IMAGE_PROMPT_PARAMS,
            "why": {"type": "string"},
        },
        "required": ["target_kind", "target_id"],
    },
    "set_description": {
        "kind": "write",
        "desc": (
            "提议给一个东西补 / 改那一句描述。**这一句是模型引用它时唯一看得到的说明**，"
            "所以只写画面里看得见的事实（外形、服装配色、材质、光线、环境），"
            "不写心理活动与剧情，不超过 120 字（超出的部分在拼 prompt 时会被截断）。"
            "先用 list_undescribed 看缺哪些、look_at_image 看一眼图，再提这条案。"
        ),
        "params": {
            "target_kind": {
                "type": "string",
                "enum": list(DESC_TARGETS),
                "description": "写到哪一种东西上（素材本身最要紧——那才是模型看的那张图）",
            },
            "target_id": {
                "type": "string",
                "description": "那一行的 id：资产 / 角色 / 形象 / 地点 / 变体 / 道具各取自己的",
            },
            "description": {"type": "string", "description": "那一句描述；给空字符串表示清掉"},
            "why": {"type": "string"},
        },
        "required": ["target_kind", "target_id", "description"],
    },
}

READ_TOOLS = tuple(name for name, spec in TOOLS.items() if spec["kind"] == "read")
WRITE_TOOLS = tuple(name for name, spec in TOOLS.items() if spec["kind"] == "write")


def tool_specs() -> list[dict[str, Any]]:
    """OpenAI 兼容的 tools 数组。只在这里拼一次，别在 agent 里再抄一份。"""
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": spec["desc"],
                "parameters": {
                    "type": "object",
                    "properties": spec["params"],
                    "required": spec.get("required", []),
                },
            },
        }
        for name, spec in TOOLS.items()
    ]


# --- 读工具：立刻执行，没有副作用 ---


def _has_appearance_desc(row: dict[str, Any]) -> bool:
    """这个形象有没有「长什么样」的文字。

    看的是 `APPEARANCE_DESC_FIELDS`——**账单里真正拼进 prompt 的就是那几格**
    （`services/context.py::_appearance_desc`）。这里另立一套判断的话，模型会看到
    「已经有描述了」而实际喂给模型的那一条其实是空的。
    """
    return any(str(row.get(f) or "").strip() for f in APPEARANCE_DESC_FIELDS)


async def run_read(pid: str, name: str, args: dict[str, Any]) -> Any:
    """执行一个读工具。返回值直接回给模型，所以只给它需要的字段——
    把整张表塞回去只会挤爆上下文，还让它更容易挑错 id。"""
    if name == "list_characters":
        out = []
        for char in await cast.list_characters(pid):
            apps = await cast.list_appearances(pid, char["id"])
            out.append(
                {
                    "id": char["id"],
                    "name": char["name"],
                    #: 那一句设定。空的时候引用这个角色只剩一个名字，所以照实回给模型看。
                    "description": str(char.get("description") or "").strip(),
                    "appearances": [
                        {
                            "id": a["id"],
                            "name": a["name"],
                            "is_default": bool(a.get("is_default")),
                            "has_sheet": bool(a.get("current_sheet")),
                            "has_description": _has_appearance_desc(a),
                        }
                        for a in apps
                    ],
                }
            )
        return out
    if name == "list_locations":
        return [
            {
                "id": loc["id"],
                "name": loc["name"],
                "description": str(loc.get("description") or "").strip(),
                "variants": [
                    {
                        "id": v["id"],
                        "name": v["name"],
                        "description": str(v.get("description") or "").strip(),
                    }
                    for v in loc["variants"]
                ],
            }
            for loc in await world.list_locations(pid)
        ]
    if name == "list_props":
        return [
            {
                "id": p["id"],
                "name": p["name"],
                "description": str(p.get("description") or "").strip(),
            }
            for p in await world.list_props(pid)
        ]
    if name == "list_undescribed":
        #: 只读清单，转调已有服务（`assets.undescribed`），这里不另算一遍「什么算缺描述」。
        return await assets.undescribed(pid)
    if name == "look_at_image":
        #: 看图是**读**：`describe.suggest` 一行库都不改，回的是建议文字。
        #: 落库仍然只有 `set_description` 提案 + 用户点采用那一条路。
        out = await describe.suggest(pid, [str(args.get("asset_id") or "")])
        row = (out.get("items") or [{}])[0]
        return {
            "asset_id": row.get("asset_id"),
            "label": row.get("label"),
            "suggestion": row.get("suggestion") or "",
            #: `vision` = 真看了图；`text` = 没送字节，只按名字与已有设定写；
            #: `skipped` = 非图片素材，没看。模型该知道这一句可信到什么程度。
            "source": row.get("source"),
            "current_description": row.get("description") or "",
            "warnings": row.get("warnings") or [],
            "error": row.get("error"),
        }
    if name == "list_scenes":
        scenes = await story.list_scenes(pid)
        links = await sequence.list_links(pid)
        return {
            "scenes": [
                {
                    "id": s["id"],
                    "index_no": s["index_no"],
                    "title": s["title"],
                    "summary": s["summary"],
                    "time_of_day": s["time_of_day"],
                    "location_variant_id": s["location_variant_id"],
                    "location": s["location_variant_name"],
                    "shot_count": s["shot_count"],
                }
                for s in scenes
            ],
            "links": [
                {
                    "from_scene_id": link["from_scene_id"],
                    "to_scene_id": link["to_scene_id"],
                    "mode": link["mode"],
                    "duration": link["duration"],
                }
                for link in links
            ],
        }
    if name == "get_scene":
        sid = str(args.get("scene_id") or "")
        lane = next((la for la in await story.storyboard(pid) if la["id"] == sid), None)
        if lane is None:
            raise AppError(
                ErrorCode.NOT_FOUND,
                "没有这一幕",
                f"scene_id = {sid or '（空）'}。",
                ["先调 list_scenes 拿到正确的 id"],
            )
        return lane
    if name == "read_script":
        return await _read_script(pid, args)
    if name == "read_skill":
        return {"skill": args.get("name"), "text": skills.render(args.get("name", ""))}
    raise AppError(
        ErrorCode.VALIDATION_ERROR,
        "不认识这个工具",
        f"{name} 不在工具白名单里。",
        [f"可用的工具：{'、'.join(TOOLS)}"],
    )


async def _read_script(pid: str, args: dict[str, Any]) -> dict[str, Any]:
    """读剧本原文的一段。**分段读是「不再超时」的关键**，所以这里只回一段，
    并且把「还剩多少」写清楚——模型靠 `next_offset` / `done` 自己决定要不要再读一段。"""
    text = str((await story.get_story(pid)).get("raw_text") or "")
    if not text.strip():
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "剧本原文是空的",
            "这个工程还没有存过剧本原文。",
            ["在剧本页左栏把剧本贴进去并保存", "或者直接告诉我剧情，我按你说的提幕与镜头"],
        )
    total = len(text)
    try:
        offset = max(0, int(args.get("offset") or 0))
        limit = int(args.get("limit") or SCRIPT_CHUNK)
    except (TypeError, ValueError) as exc:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "offset / limit 只能是整数",
            f"offset={args.get('offset')!r}，limit={args.get('limit')!r}：{exc}",
            ["第一次读给 offset=0，之后用上一次返回的 next_offset"],
        ) from exc
    limit = min(max(1, limit), SCRIPT_CHUNK_MAX)
    if offset >= total:
        return {"total": total, "offset": total, "next_offset": total, "done": True, "text": ""}
    chunk = text[offset : offset + limit]
    nxt = offset + len(chunk)
    return {
        "total": total,
        "offset": offset,
        "next_offset": nxt,
        "done": nxt >= total,
        "text": chunk,
    }


# --- 写工具：只翻译成提案，永不落库 ---


def _clean(args: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    """只留模型真的给了的字段。给了 None 等于没给——不能把已有的概要清空。"""
    return {k: args[k] for k in keys if args.get(k) is not None}


async def _resolve_appearances(pid: str, args: dict[str, Any]) -> tuple[list[dict], list[str]]:
    """角色名 / 形象 id → 形象。对不上的名字进 warnings，不静默丢。"""
    picked: list[dict[str, Any]] = []
    warnings: list[str] = []
    chars = await run_read(pid, "list_characters", {})
    by_app = {a["id"]: (c, a) for c in chars for a in c["appearances"]}
    for aid in args.get("appearance_ids") or []:
        hit = by_app.get(str(aid))
        if hit is None:
            warnings.append(f"形象 id {aid} 不存在，这一条会被忽略")
            continue
        char, app = hit
        picked.append({"appearance_id": app["id"], "label": f"{char['name']} · {app['name']}"})
    for raw in args.get("character_names") or []:
        name = str(raw).strip()
        char = next((c for c in chars if c["name"] == name), None) or next(
            (c for c in chars if name and (name in c["name"] or c["name"] in name)), None
        )
        if char is None or not char["appearances"]:
            warnings.append(f"「{name}」对不上任何角色，先在角色工作台建一个")
            continue
        app = next((a for a in char["appearances"] if a["is_default"]), char["appearances"][0])
        picked.append({"appearance_id": app["id"], "label": f"{char['name']} · {app['name']}"})
    seen: set[str] = set()
    unique = [p for p in picked if not (p["appearance_id"] in seen or seen.add(p["appearance_id"]))]
    return unique, warnings


async def _resolve_props(pid: str, args: dict[str, Any]) -> tuple[list[dict], list[str]]:
    props = await world.list_props(pid)
    picked: list[dict[str, Any]] = []
    warnings: list[str] = []
    for pid_raw in args.get("prop_ids") or []:
        hit = next((p for p in props if p["id"] == str(pid_raw)), None)
        if hit is None:
            warnings.append(f"道具 id {pid_raw} 不存在，这一条会被忽略")
            continue
        picked.append({"prop_id": hit["id"], "label": hit["name"]})
    for raw in args.get("prop_names") or []:
        name = str(raw).strip()
        hit = next((p for p in props if p["name"] == name), None) or next(
            (p for p in props if name and (name in p["name"] or p["name"] in name)), None
        )
        if hit is None:
            warnings.append(f"「{name}」对不上任何道具，先在道具库建一个")
            continue
        picked.append({"prop_id": hit["id"], "label": hit["name"]})
    seen: set[str] = set()
    return [p for p in picked if not (p["prop_id"] in seen or seen.add(p["prop_id"]))], warnings


async def _scene(pid: str, sid: str) -> dict[str, Any]:
    """按 id 取一幕。取不到就报错回给模型——它可以重新调 list_scenes 拿对的 id，
    这比编一条指向不存在的幕的提案好。"""
    hit = next((s for s in await story.list_scenes(pid) if s["id"] == str(sid)), None)
    if hit is None:
        raise AppError(
            ErrorCode.NOT_FOUND,
            "没有这一幕",
            f"scene_id = {sid or '（空）'}。",
            ["先调 list_scenes 拿到正确的 id"],
        )
    return hit


async def _shot(pid: str, shot_id: str) -> dict[str, Any]:
    """按 id 取一个镜头。取不到报错回给模型（它可以 get_scene 再看一遍镜头清单）。"""
    try:
        return await story.get_shot(pid, str(shot_id or ""))
    except AppError as exc:
        if exc.code is not ErrorCode.NOT_FOUND:
            raise
        raise AppError(
            ErrorCode.NOT_FOUND,
            "没有这个镜头",
            f"shot_id = {shot_id or '（空）'}。",
            ["先调 get_scene 看这一幕的镜头清单，用里面的 id"],
        ) from exc


#: 镜头上「直接落库」的那几个字段（`story.SHOT_FIELDS` 的子集）。
SHOT_PLAIN_KEYS = ("title", "description", "duration", "camera", "movement")
#: prompt 那三段。顺序即 `format_shot_prompt` 的段落顺序。
SHOT_SEGMENT_KEYS = ("camera_motion", "visual_prompt", "audio_dialogue")


async def _shot_after(
    pid: str,
    args: dict[str, Any],
    index: int,
    before: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[str]]:
    """把镜头类写工具的参数拼成提案的 `after`。**正向 prompt 只在这里拼一次。**

    三段里没给的那几段从 `before` 的 prompt 里解析出来接着用——改一个镜头时模型往往只给
    `visual_prompt`，直接重拼会把原来的机位与对白抹成默认值。
    """
    warnings: list[str] = []
    after: dict[str, Any] = _clean(args, SHOT_PLAIN_KEYS)
    if "duration" in after:
        after["duration"] = float(after["duration"])

    segs = dict(prompts.parse_shot_prompt(str((before or {}).get("prompt") or "")))
    given = {k: str(args[k]).strip() for k in SHOT_SEGMENT_KEYS if args.get(k) is not None}
    segs.update({k: v for k, v in given.items() if v})
    if not segs.get("camera_motion"):
        camera = str(args.get("camera") or (before or {}).get("camera") or "").strip()
        movement = str(args.get("movement") or (before or {}).get("movement") or "").strip()
        if camera or movement:
            segs["camera_motion"] = "，".join(x for x in (camera, movement) if x)
    fallback = str(
        after.get("description") or (before or {}).get("description") or after.get("title") or ""
    )
    prompt, negative = prompts.with_shot_audio_policy(
        prompts.format_shot_prompt(
            index,
            segs.get("camera_motion", ""),
            segs.get("visual_prompt", ""),
            segs.get("audio_dialogue", ""),
            fallback,
        ),
        str(args.get("negative_prompt") or (before or {}).get("negative_prompt") or ""),
    )
    after.update({k: segs[k] for k in SHOT_SEGMENT_KEYS if k in segs})
    after["prompt"] = prompt
    after["negative_prompt"] = negative

    if args.get("skill") is not None:
        declared = str(args["skill"]).strip().lower()
        if declared not in skills.NAMES:
            warnings.append(f"skill「{declared}」不是内置的那四份，只当备注看")
        after["skill"] = declared
        expected = skills.pick(
            bool((before or {}).get("first_frame_asset_id")),
            bool((before or {}).get("last_frame_asset_id")),
        )
        if before is not None and declared in skills.NAMES and declared != expected:
            warnings.append(
                f"这个镜头挂的图对应 {expected} 那一份，但 prompt 是照 {declared} 写的——"
                "锚定语可能与实际首 / 末帧不符"
            )

    if args.get("character_names") is not None or args.get("appearance_ids") is not None:
        picked, warn = await _resolve_appearances(pid, args)
        after["cast"] = picked
        warnings.extend(warn)
    return after, warnings


#: 每个写工具动的是哪一类东西。前端按 target 分组显示提案，所以这张表是它的唯一来源。
_TARGET = {
    "set_link": "link",
    "add_shot": "shot",
    "update_shot": "shot",
    "delete_shot": "shot",
    "reorder_shots": "shot",
    "set_shot_link": "shot_link",
    "add_character": "material",
    "add_location": "material",
    "add_prop": "material",
    "generate_reference": "material",
    "set_description": "material",
}

#: 图片服务没配置时那句话。**绝不静默跳过**：素材照样建得出来，图不会有，
#: 所以这条 warning 必须跟着提案一起给用户看（照 CLAUDE.md 硬约束 4）。
IMAGE_OFF_WARNING = "图片服务未配置：只会建素材，不会生成图。去设置页配一个，或手动导入图片"


#: 出图那四个工具共用这一支。**正 / 负向 prompt 只在这里拼一次**
#: （照 `_shot_after()` 的老规矩），落库那边只照 `after` 用，不再拼第二遍。
def _image_after(
    op: dict[str, Any],
    args: dict[str, Any],
    target_kind: str,
) -> dict[str, Any]:
    """把 `image_prompt` + `skill` 拼成提案里的那几个字段。

    没给 `image_prompt` 就是「只建素材、不出图」——这不是错误，用户后面还能在素材页
    点「生成参考图」。给了但图片服务没配置时照样把 prompt 拼好放进 `after`
    （用户看得见 AI 想出什么图），只是多一条 warning 说明这一次不会真出图。
    """
    text = str(args.get("image_prompt") or "").strip()
    declared = str(args.get("skill") or "").strip().lower()
    if declared and declared not in skills.IMAGE_NAMES:
        op["warnings"].append(f"skill「{declared}」不是内置的那三份出图 SKILL，已按素材类型自动选")
        declared = ""
    name = declared or skills.image_pick(target_kind)
    out: dict[str, Any] = {"skill": name, "image_prompt": text, "generate_image": bool(text)}
    if not text:
        return out
    positive, negative = skills.render_image_prompt(name, text)
    out["prompt"] = positive
    out["negative_prompt"] = negative
    if not registry.image_configured():
        out["generate_image"] = False
        op["warnings"].append(IMAGE_OFF_WARNING)
    return out


async def to_op(pid: str, name: str, args: dict[str, Any], temp_no: int) -> dict[str, Any]:
    """把一次写工具调用翻译成一条提案。**这里绝不碰数据库的写路径。**"""
    if name not in WRITE_TOOLS:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "不认识这个工具",
            f"{name} 不是写工具。",
            [f"可用的写工具：{'、'.join(WRITE_TOOLS)}"],
        )
    why = str(args.get("why") or "").strip()
    op: dict[str, Any] = {
        "op": name,
        "target": _TARGET.get(name, "scene"),
        "temp_id": f"op{temp_no}",
        "before": None,
        "after": {},
        "why": why,
        "warnings": [],
    }

    if name == "add_scene":
        shots: list[dict[str, Any]] = []
        for raw in args.get("shots") or []:
            if not isinstance(raw, dict):
                continue
            after, warn = await _shot_after(pid, raw, len(shots) + 1, None)
            op["warnings"].extend(warn)
            shots.append(
                {
                    "title": str(raw.get("title") or f"镜头 {len(shots) + 1}"),
                    "duration": float(raw.get("duration") or 4.0),
                    **after,
                }
            )
        op["after"] = {
            **_clean(args, ("title", "summary", "time_of_day", "location_variant_id")),
            "shots": shots,
        }
        return op

    if name == "update_scene":
        row = await _scene(pid, args.get("scene_id", ""))
        keys = ("title", "summary", "time_of_day", "location_variant_id")
        op["scene_id"] = row["id"]
        op["before"] = {k: row[k] for k in keys}
        op["after"] = _clean(args, keys)
        return op

    if name == "set_scene_prompt":
        row = await _scene(pid, args.get("scene_id", ""))
        op["scene_id"] = row["id"]
        op["before"] = {"title": row["title"], "shot_count": row["shot_count"]}
        op["after"] = {"prompt": str(args.get("prompt") or "")}
        return op

    if name == "set_scene_cast":
        row = await _scene(pid, args.get("scene_id", ""))
        picked, warn = await _resolve_appearances(pid, args)
        op["scene_id"] = row["id"]
        op["before"] = {"title": row["title"], "shot_count": row["shot_count"]}
        op["after"] = {"cast": picked}
        op["warnings"].extend(warn)
        return op

    if name == "set_scene_props":
        row = await _scene(pid, args.get("scene_id", ""))
        picked, warn = await _resolve_props(pid, args)
        op["scene_id"] = row["id"]
        op["before"] = {"title": row["title"], "shot_count": row["shot_count"]}
        op["after"] = {"props": picked}
        op["warnings"].extend(warn)
        return op

    if name == "set_link":
        head = await _scene(pid, args.get("from_scene_id", ""))
        tail = await _scene(pid, args.get("to_scene_id", ""))
        mode = str(args.get("mode") or "")
        if mode not in LINK_MODES:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "未知的衔接方式",
                f"{mode or '（空）'} 不在 {'、'.join(LINK_MODES)} 里。",
                ["mode 只能是 cut / transition / tail_frame"],
            )
        existing = next(
            (
                link
                for link in await sequence.list_links(pid)
                if link["from_scene_id"] == head["id"] and link["to_scene_id"] == tail["id"]
            ),
            None,
        )
        op["before"] = (
            {"mode": existing["mode"], "duration": existing["duration"]} if existing else None
        )
        op["after"] = {
            "from_scene_id": head["id"],
            "to_scene_id": tail["id"],
            "from_title": head["title"],
            "to_title": tail["title"],
            "mode": mode,
            **_clean(args, ("duration", "prompt")),
        }
        return op

    if name == "reorder_scenes":
        rows = await story.list_scenes(pid)
        known = {s["id"]: s for s in rows}
        order = [str(i) for i in args.get("order") or [] if str(i) in known]
        missing = [s["id"] for s in rows if s["id"] not in order]
        if missing:
            op["warnings"].append(f"有 {len(missing)} 幕没出现在新顺序里，会按原顺序排在后面")
        op["before"] = {"order": [f"{s['index_no']}. {s['title']}" for s in rows]}
        op["after"] = {
            "order": order + missing,
            "titles": [known[i]["title"] for i in order + missing],
        }
        return op

    if name == "add_shot":
        row = await _scene(pid, args.get("scene_id", ""))
        count = int(row["shot_count"] or 0)
        raw_pos = args.get("position")
        position = None if raw_pos is None else max(1, min(int(raw_pos), count + 1))
        after, warn = await _shot_after(pid, args, position or count + 1, None)
        op["scene_id"] = row["id"]
        op["before"] = {"scene_title": row["title"], "shot_count": count}
        op["after"] = {
            "scene_id": row["id"],
            "title": str(args.get("title") or f"镜头 {position or count + 1}"),
            "duration": float(args.get("duration") or 4.0),
            **after,
            **({"position": position} if position else {}),
        }
        op["warnings"].extend(warn)
        return op

    if name == "update_shot":
        row = await _shot(pid, args.get("shot_id", ""))
        after, warn = await _shot_after(pid, args, int(row["index_no"] or 1), row)
        op["shot_id"] = row["id"]
        op["before"] = {
            "scene_title": row["scene_title"],
            "index_no": row["index_no"],
            **{k: row.get(k) for k in SHOT_PLAIN_KEYS},
            "prompt": row.get("prompt"),
            "negative_prompt": row.get("negative_prompt"),
            "cast": [
                {"appearance_id": c["appearance_id"], "label": c.get("character_name") or ""}
                for c in row.get("cast") or []
            ],
        }
        op["after"] = after
        op["warnings"].extend(warn)
        if row.get("version_count"):
            op["warnings"].append(
                f"这个镜头已经生成过 {row['version_count']} 版；改 prompt 不影响已有版本，"
                "要看新写法的效果得再生成一次"
            )
        return op

    if name == "delete_shot":
        row = await _shot(pid, args.get("shot_id", ""))
        op["shot_id"] = row["id"]
        op["before"] = {
            "scene_title": row["scene_title"],
            "index_no": row["index_no"],
            "title": row["title"],
            "version_count": row.get("version_count"),
        }
        op["after"] = None
        if row.get("version_count"):
            op["warnings"].append(f"它生成过 {row['version_count']} 版，删掉后这些版本一起没")
        return op

    if name == "reorder_shots":
        row = await _scene(pid, args.get("scene_id", ""))
        lane = await run_read(pid, "get_scene", {"scene_id": row["id"]})
        rows = [s for s in lane.get("shots") or [] if s.get("kind") != "transition"]
        known = {s["id"]: s for s in rows}
        order = [str(i) for i in args.get("order") or [] if str(i) in known]
        missing = [s["id"] for s in rows if s["id"] not in order]
        if missing:
            op["warnings"].append(f"有 {len(missing)} 个镜头没出现在新顺序里，会按原顺序排在后面")
        op["scene_id"] = row["id"]
        op["before"] = {"order": [f"{s['index_no']}. {s['title']}" for s in rows]}
        op["after"] = {
            "scene_id": row["id"],
            "order": order + missing,
            "titles": [known[i]["title"] for i in order + missing],
        }
        return op

    if name == "set_shot_link":
        head = await _shot(pid, args.get("from_shot_id", ""))
        tail = await _shot(pid, args.get("to_shot_id", ""))
        mode = str(args.get("mode") or "")
        if mode not in SHOT_LINK_MODES:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "未知的镜头衔接方式",
                f"{mode or '（空）'} 不在 {'、'.join(SHOT_LINK_MODES)} 里。",
                [
                    "mode 只能是 cut / transition",
                    "「续接上游末帧」请改 update_shot 的上游依赖，镜头级没有 tail_frame",
                ],
            )
        existing = next(
            (
                link
                for link in await sequence.list_shot_links(pid)
                if link["from_shot_id"] == head["id"] and link["to_shot_id"] == tail["id"]
            ),
            None,
        )
        op["before"] = (
            {"mode": existing["mode"], "duration": existing["duration"]} if existing else None
        )
        op["after"] = {
            "from_shot_id": head["id"],
            "to_shot_id": tail["id"],
            "from_title": head["title"],
            "to_title": tail["title"],
            "mode": mode,
            **_clean(args, ("duration", "prompt")),
        }
        return op

    if name == "add_character":
        raw = str(args.get("name") or "").strip()
        existing = [c for c in await cast.list_characters(pid) if c["name"] == raw]
        if existing:
            op["warnings"].append(f"工程里已经有一个叫「{raw}」的角色，采用后会多出一个同名角色")
        op["after"] = {
            **_clean(args, ("name", "description")),
            **_image_after(op, args, "appearance"),
        }
        return op

    if name == "add_location":
        raw = str(args.get("name") or "").strip()
        if any(loc["name"] == raw for loc in await world.list_locations(pid)):
            op["warnings"].append(f"工程里已经有一个叫「{raw}」的地点，采用后会多出一个同名地点")
        op["after"] = {
            **_clean(args, ("name", "description")),
            "variant": str(args.get("variant") or "").strip() or "默认场景",
            **_clean(args, ("time_of_day",)),
            **_image_after(op, args, "location_variant"),
        }
        return op

    if name == "add_prop":
        raw = str(args.get("name") or "").strip()
        if any(p["name"] == raw for p in await world.list_props(pid)):
            op["warnings"].append(f"工程里已经有一个叫「{raw}」的道具，采用后会多出一个同名道具")
        op["after"] = {
            **_clean(args, ("name", "description")),
            **_image_after(op, args, "prop"),
        }
        return op

    if name == "generate_reference":
        kind = str(args.get("target_kind") or "").strip()
        if kind not in IMAGE_TARGETS:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "不认识这种素材类型",
                f"target_kind = {kind or '（空）'}。",
                [f"可用的是：{'、'.join(IMAGE_TARGETS)}"],
                {"target_kind": args.get("target_kind")},
            )
        target_id = str(args.get("target_id") or "").strip()
        label = await images.target_label(pid, kind, target_id)
        if label is None:
            raise AppError(
                ErrorCode.NOT_FOUND,
                "没有这个素材",
                f"{kind} 里没有 id = {target_id or '（空）'} 这一行。",
                [
                    "先调 list_characters / list_locations / list_props 拿正确的 id",
                    "形象出图给的是**形象 id**，不是角色 id；地点给的是**变体 id**",
                ],
                {"target_kind": kind, "target_id": target_id},
            )
        if not str(args.get("image_prompt") or "").strip():
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "没写这张图长什么样",
                "generate_reference 的 image_prompt 是空的，那样只能出一张没有细节的图。",
                ["写一句「长什么样」：外形、服装配色、材质、时间与天气"],
            )
        op["after"] = {
            "target_kind": kind,
            "target_id": target_id,
            "target_label": label,
            **_image_after(op, args, kind),
        }
        return op

    if name == "set_description":
        kind = str(args.get("target_kind") or "").strip()
        target_id = str(args.get("target_id") or "").strip()
        if kind not in DESC_TARGETS:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "不认识这种目标",
                f"target_kind = {kind or '（空）'}。",
                [f"可用的是：{'、'.join(DESC_TARGETS)}"],
                {"target_kind": args.get("target_kind")},
            )
        row = await describe.target(pid, kind, target_id)
        if row is None:
            raise AppError(
                ErrorCode.NOT_FOUND,
                f"没有这个{DESC_TARGET_LABEL[kind]}",
                f"{kind} 里没有 id = {target_id or '（空）'} 这一行。",
                [
                    "先调 list_undescribed / list_characters / list_locations / list_props "
                    "拿正确的 id",
                    "形象给的是**形象 id**，不是角色 id；地点变体给的是**变体 id**",
                ],
                {"target_kind": kind, "target_id": target_id},
            )
        text = " ".join(str(args.get("description") or "").split())
        if len(text) > DESC_MAX:
            op["warnings"].append(
                f"这一句有 {len(text)} 字，拼进 prompt 时只会带前 {DESC_MAX} 字，超出的部分白写"
            )
        op["before"] = {
            "target_label": row["label"],
            "field": row["field"],
            "description": row["description"],
        }
        op["after"] = {
            "target_kind": kind,
            "target_id": row["id"],
            "target_label": row["label"],
            #: 写哪一列由 `describe.target` 说（形象上没有 description 列）。
            #: 落库那边照这个字段 patch，绝不在 `services/director.py` 里再认一遍 kind。
            "field": row["field"],
            "description": text,
        }
        if kind == "appearance":
            op["warnings"].append(
                "形象上没有「描述」这一列，这一句会写进「显著特征」（traits）——"
                "那正是账单拼进 prompt 时读的那几格"
            )
        if not text and row["description"]:
            op["warnings"].append("description 是空的：采用后会把现在那一句清掉")
        return op

    row = await _scene(pid, args.get("scene_id", ""))  # delete_scene
    op["scene_id"] = row["id"]
    op["before"] = {"title": row["title"], "shot_count": row["shot_count"]}
    op["after"] = None
    if row["shot_count"]:
        op["warnings"].append(f"这一幕下有 {row['shot_count']} 个镜头，删掉后它们的版本一起没")
    return op

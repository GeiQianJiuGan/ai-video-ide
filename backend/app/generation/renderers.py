"""SKILL 渲染库：把一次生成请求渲染成**某个模型**的正向 prompt 形状。

这是「导演两层重构」的**规范层**。创作层（AI 导演）产的是**模型无关**的东西——四段
`segments`（往后是 `intent_json`）；规范层按工程选定的 skill 名把它翻成该模型认得的 prompt。
为什么要独立成这一层（而不是把 H3 六段焊在 `base.py` 里）：用户的模型不一定是 MiniMax H3，
把「怎么排 prompt」写死在代码里，等于在 prompt 层重演硬约束 1 本要杜绝的「业务绑具体模型」。

**这一层是纯生成层**：只吃一个 `VideoRequest` + `PictureBook`，吐一段字符串，
**绝不 import `app.ai`**（分层方向是 ai → generation）。选哪个渲染器由 `req.skill` 决定，
那个名字是入队时工程路由解析并冻结的（`route.skill`）。

**分岔只有 `render_prompt` 这一处**（照硬约束 1「按名字分岔只有一处」的作风）：

  - `minimax-h3`（默认）——MiniMax H3 官方六段结构，主体逻辑仍在
    `base.render_video_prompt`（那儿还有十几个 H3 专用的排版助手），这里只是薄壳转调，
    避免把两百多行搬家带来的回归风险；
  - `generic`——自由散文兜底：不认识具体模型形状时，把画面 / 机位 / 声音顺成一段可读的
    英文散文，末尾补一句「谁是谁」的对号表（`book.subjects`）。一线的自由文本模型照样能用。

未知 skill 名不静默换默认再不吭声：折成 `minimax-h3` 并往 `req.notes` 记一条
（硬约束 4，降级也要说出来并冻结进版本参数）。
"""

from __future__ import annotations

from app.generation import intent as gen_intent
from app.generation.providers import base
from app.generation.providers.base import PictureBook, VideoRequest, clip_desc

#: 现在发的渲染器。加一家模型只在这里多一个名字 + 一个 `render_*` 函数，别处不动。
SKILLS: tuple[str, ...] = ("minimax-h3", "generic")
#: skill 三级回退最后那一档的默认值（与 `settings.video_skill` / `route.skill_name_of` 同源）。
DEFAULT_SKILL = "minimax-h3"

#: 常见别名折到规范名上（与 `ai/skills` 那张 `ALIASES` 精神一致，但这一层不认那张表——
#: 它属于 ai 侧，这里只认渲染器名）。未列出的名字走 `render_prompt` 的未知分支。
_ALIASES = {
    "minimax_h3": "minimax-h3",
    "minimax": "minimax-h3",
    "h3": "minimax-h3",
    "h3-prompt-writing": "minimax-h3",
    "h3-base": "minimax-h3",
    "h3-ref": "minimax-h3",
    "": DEFAULT_SKILL,
}


def normalize(skill: str) -> str:
    """把一个 skill 名折成规范名。认不出照原样返回（由 `render_prompt` 记 note）。"""
    raw = str(skill or "").strip().lower()
    return _ALIASES.get(raw, raw)


def render_h3(req: VideoRequest, book: PictureBook) -> str:
    """MiniMax H3 六段。主体逻辑在 `base.render_video_prompt`，这里只转调。"""
    return base.render_video_prompt(req, book)


def render_generic(req: VideoRequest, book: PictureBook) -> str:
    """自由散文兜底：把画面 / 机位 / 声音顺成一段可读文本，末尾补对号表。

    只有 `comfy_base._retold` 在「有 subjects 且开着标签」时才会走到渲染这一步，所以这里
    默认能拿到 `book.subjects`——把「谁是谁」用一句话摆出来，让收不到结构化字段的模型也能
    按名认人（措辞与 H3 的 `subject_definitions` 刻意不同：那一份要让模型认得住，这一份是
    通用模型的自由文本，越像人话越好）。
    """
    segments = req.segments or {}
    shot_no = max(1, int(req.shot_no or 1))
    visual = str(segments.get("visual_prompt") or req.prompt or "").strip()
    camera = str(segments.get("camera_motion") or "").strip()
    audio = str(segments.get("audio_dialogue") or "").strip()

    parts: list[str] = [f"[Shot {shot_no}]"]
    if visual:
        parts.append(visual)
    if camera:
        parts.append(f"Camera: {camera}")
    if audio:
        parts.append(f"Audio: {audio}")
    # 无配乐：与 H3 的 `non_diegetic_music: none` 同一句口径，只不过这里写成自由文本。
    parts.append("No background music or score.")

    defs = [
        f"{p.name}（{clip_desc(p.desc)}）" if p.desc else p.name for p in book.subjects if p.name
    ]
    if defs:
        parts.append("Subjects: " + "；".join(defs) + "。")
    return "\n".join(parts)


#: 规范名 → 渲染函数。`render_prompt` 只查这一张表。
_RENDERERS = {
    "minimax-h3": render_h3,
    "generic": render_generic,
}


def render_prompt(skill: str, req: VideoRequest, book: PictureBook) -> str:
    """按 skill 名选一个渲染器把请求渲染成正向 prompt。**全应用按 skill 分岔只有这一处。**

    有意图就先过 `intent.to_segments` 这座**唯一的桥**覆盖到 `segments` 上（两个渲染器都吃
    `segments`，不认识 intent 这个词）——同一份意图配不同 skill 就渲染成不同模型的形状。
    没意图（老工程 / Manual）就用入队时拆好的 `segments` / `prompt`，**输出与升级前逐字相同**。

    未知名字折成 `minimax-h3` 并往 `req.notes` 记一条（硬约束 4）：谎报「按你的模型渲染了」
    比明说「不认识这个 skill，退回了默认那份」糟得多——后者一路冻进版本参数，事后翻得到。
    """
    if req.intent:
        segs = gen_intent.to_segments(req.intent)
        if any(segs.values()):
            req.segments = segs
    name = normalize(skill)
    renderer = _RENDERERS.get(name)
    if renderer is None:
        req.notes.append(
            f"不认识渲染 skill「{skill}」，这次退回默认的 {DEFAULT_SKILL} 渲染器"
            f"（可选：{'、'.join(SKILLS)}）。"
        )
        renderer = _RENDERERS[DEFAULT_SKILL]
    return renderer(req, book)

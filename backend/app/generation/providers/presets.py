"""ComfyUI 预设：模型端那份图的本地副本 + 入口约定。

这是「本工具不维护模型端的图」这条约束的落点。做法只有一条约定：

    用户在 ComfyUI 里把入口节点的**标题**改成 AIVS_FIRST_FRAME / AIVS_LAST_FRAME /
    AIVS_PROMPT / AIVS_NEGATIVE / AIVS_DURATION / AIVS_SEED / AIVS_REF_1…AIVS_REF_9 /
    AIVS_REF_VIDEO_1…4 / AIVS_REF_AUDIO_1…4，然后导出 API 格式的 json。

`AIVS_REF_*` 是**参考素材**槽位，与首尾帧分开：首尾帧是「画面从哪一格开始 / 结束」，
参考素材是「谁出场、在哪儿、动作什么样、跟着哪段声音」。只有首帧时人物形象只能靠那一张
图带，很容易在几秒里跑掉——所以账单里算出来的角色表 / 地点参考图按序号填进这些槽位。
图里标了几个就用几个，一个都没标也能生成（只是丢形象的风险照旧）。

**参考素材分三种媒体，各有各的槽位**，因为模型端接它们的节点根本不是一个：
图片进 `AIVS_REF_n`（LoadImage 那类），视频进 `AIVS_REF_VIDEO_n`（VHS / LoadVideo 那类），
音频进 `AIVS_REF_AUDIO_n`（LoadAudio 那类）。混在一起数会把一段 `.mp4` 填进 LoadImage，
那既不报错也出不了片——所以槽位、上限、降级说明全部按媒体分开算。

**首尾帧槽位也是可选的**：分工是「首尾帧那类模型补转场，R2V 出正片」，而能收多参考图的
R2V 图往往根本没有首帧入口。所以必需入口只剩 `AIVS_PROMPT` 一个——没有
`AIVS_FIRST_FRAME` 的图照样能存、能选、能生成，那张首帧会当参考图 1 送进去
（降级说明写进 `req.notes`，界面上看得见），只有转场（严格首尾帧）需要标全三个。

我们只按标题找这几个节点、只往里填值。图里挂了多少 lora、加了什么加速节点、
采样器换成了什么——一概不看、不校验、不改写。模型端想怎么调就怎么调，
本工具不需要跟着更新任何绑定表（这正是旧 Workflow 绑定路径太重的地方）。

预设文件放 `runtime_dir/presets/<名字>.json`：它属于「我这台机器怎么调模型」，
不是工程数据，所以不进 project.db，跟着应用级设置走。

**一份图不只能出画面**：标了 `AIVS_SOURCE_VIDEO` 的是二次处理图（超分 / 插帧 / 重做尾段，
输入是已经出好的那一段），标了 `AIVS_AUDIO_TEXT` / `AIVS_AUDIO_PROMPT` 的是音源图
（另一条链，见 `providers/audio.py`）。所以 `inspect()` 回的 `ready` 是「至少能做一件事」，
音源图与超分图不需要 `AIVS_PROMPT`——按老口径它们连保存都过不了。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.generation.providers.base import RefCapacity

#: 入口标题 → 该往节点的哪个输入里填。按顺序取第一个命中的键，
#: 这样 LoadImage / CLIPTextEncode / 各家的原生节点都能覆盖，而不必认识它们的 class_type。
IMAGE_FIELDS = ("image", "filename", "url", "value")
#: 视频参考素材的入口：VHS_LoadVideoPath 是 `video`、核心 LoadVideo 是 `file`，
#: 其余各家用 filename / path。和图片分开是因为它们压根不是同一类节点。
VIDEO_FIELDS = ("video", "file", "filename", "path", "url", "value")
#: 音频参考素材的入口：LoadAudio 是 `audio`（它还有个只读的 audioUI，刻意不碰）。
AUDIO_FIELDS = ("audio", "file", "filename", "path", "url", "value")

#: 参考素材槽位的上限。9 是「一眼能数清」的数目，也刚好覆盖多参考图模型的常见入参
#: （例如 ref_image_0..8）。图里有几个就用几个，不必凑满。
REF_SLOTS = 9
#: 视频 / 音频参考素材的槽位上限。比图片少：能收多段视频或多轨音频的图极少见，
#: 4 个已经够 VACE 那类「参考视频 + 遮罩视频」和 S2V 那类「一段说话音频」用。
REF_VIDEO_SLOTS = 4
REF_AUDIO_SLOTS = 4
#: 参考素材槽位的标题，按序号排好——`ref_slots()` 取的就是这个顺序。
REF_MARKERS: tuple[str, ...] = tuple(f"AIVS_REF_{i}" for i in range(1, REF_SLOTS + 1))
REF_VIDEO_MARKERS: tuple[str, ...] = tuple(
    f"AIVS_REF_VIDEO_{i}" for i in range(1, REF_VIDEO_SLOTS + 1)
)
REF_AUDIO_MARKERS: tuple[str, ...] = tuple(
    f"AIVS_REF_AUDIO_{i}" for i in range(1, REF_AUDIO_SLOTS + 1)
)
#: 媒体 → 它的槽位标题。**这是「参考素材按媒体分开」的唯一一张表**：
#: 账单算上限、适配器填槽位、UI 上那句提示都从这里取，别在别处再写一份。
REF_MARKERS_BY_MEDIA: dict[str, tuple[str, ...]] = {
    "image": REF_MARKERS,
    "video": REF_VIDEO_MARKERS,
    "audio": REF_AUDIO_MARKERS,
}
#: 媒体的中文说法，四要素错误与界面文案共用一份，别两处不一致。
MEDIA_LABEL = {"image": "参考图", "video": "参考视频", "audio": "参考音频"}
#: 媒体 → 它那一族槽位标题怎么写给人看（错误建议里那句「在图里加 … 标题」用它）。
MARKER_FAMILY = {
    "image": f"AIVS_REF_1…AIVS_REF_{REF_SLOTS}",
    "video": f"AIVS_REF_VIDEO_1…{REF_VIDEO_SLOTS}",
    "audio": f"AIVS_REF_AUDIO_1…{REF_AUDIO_SLOTS}",
}

#: 文本入口的候选字段。提示词、台词、声音描述都是同一类节点（CLIPTextEncode / 各家的
#: 文本框），所以只写一份。
TEXT_FIELDS = ("text", "prompt", "string", "value")
#: 时长与种子的候选字段。音频那份图与视频那份图用的是同一批名字。
DURATION_FIELDS = ("length", "duration", "frames", "seconds", "num_frames", "value")
SEED_FIELDS = ("seed", "noise_seed", "value")
#: 出图那份图的画幅入口。与时长共用一批候选字段名（EmptyLatentImage 是 `width` / `height`，
#: 各家的原生节点常用 `value`），所以不另造一份候选表。
SIZE_FIELDS = ("width", "height", "value", "size")

MARKERS: dict[str, tuple[str, ...]] = {
    "AIVS_FIRST_FRAME": IMAGE_FIELDS,
    "AIVS_LAST_FRAME": IMAGE_FIELDS,
    "AIVS_PROMPT": TEXT_FIELDS,
    "AIVS_NEGATIVE": TEXT_FIELDS,
    "AIVS_DURATION": DURATION_FIELDS,
    "AIVS_SEED": SEED_FIELDS,
    #: **二次处理的输入**：已经出好的那一段视频（超分 / 插帧 / 重做尾段都从它出发）。
    #: 与 `AIVS_REF_VIDEO_*` 严格分开——参考视频是「动作长这样」，源视频是「就处理这一段」。
    #: 两者混用的话，超分图会把一段参考视频当成待处理的画面，出来的东西跟这个镜头无关。
    "AIVS_SOURCE_VIDEO": VIDEO_FIELDS,
    #: 音源那份图的入口。**音频是另一条链**（另一份图、另一个地址、另一份预设），
    #: 所以它有自己的一族标题：`AIVS_PROMPT` 是画面提示词，拿它当台词只会两边打架。
    "AIVS_AUDIO_TEXT": TEXT_FIELDS,
    "AIVS_AUDIO_PROMPT": TEXT_FIELDS,
    "AIVS_VOICE_REF": AUDIO_FIELDS,
    "AIVS_AUDIO_DURATION": DURATION_FIELDS,
    "AIVS_AUDIO_SEED": SEED_FIELDS,
    #: 出图那份图的画幅入口（**另一条链**，见 `providers/image.py`）。刻意只有这两个：
    #: 提示词 / 负向 / 种子 / 参考图那几族出图与出视频用的是同一批标题，T2I 图上本来就有。
    #: **`inspect()` 不因为它们多一条 ready 判定**——从入口标题分不出「这是 T2I 还是 R2V」，
    #: 出图用哪份图靠 `image.preset` 设置指名，硬猜只会猜错。
    "AIVS_WIDTH": SIZE_FIELDS,
    "AIVS_HEIGHT": SIZE_FIELDS,
    #: 参考素材：角色表 / 地点参考图 / 动作参考视频 / 对白音频从这里进去。
    #: 首帧只能是一张，参考素材想喂几个标几个。
    **dict.fromkeys(REF_MARKERS, IMAGE_FIELDS),
    **dict.fromkeys(REF_VIDEO_MARKERS, VIDEO_FIELDS),
    **dict.fromkeys(REF_AUDIO_MARKERS, AUDIO_FIELDS),
}

#: 入口标题 → 人看得懂的说法。**只有这一份**：错误文案、设置页的手动对应表、
#: 「这一格是干什么的」都从这里取，写两份必然对不上。
MARKER_LABEL: dict[str, str] = {
    "AIVS_PROMPT": "画面提示词",
    "AIVS_NEGATIVE": "负向提示词",
    "AIVS_FIRST_FRAME": "首帧",
    "AIVS_LAST_FRAME": "末帧",
    "AIVS_DURATION": "时长 / 帧数",
    "AIVS_SEED": "随机种子",
    "AIVS_SOURCE_VIDEO": "待处理的那一段视频",
    "AIVS_AUDIO_TEXT": "台词",
    "AIVS_AUDIO_PROMPT": "声音描述",
    "AIVS_VOICE_REF": "音色参考",
    "AIVS_AUDIO_DURATION": "音频时长",
    "AIVS_AUDIO_SEED": "音频随机种子",
    "AIVS_WIDTH": "图片宽",
    "AIVS_HEIGHT": "图片高",
    **{m: f"参考图 {i + 1}" for i, m in enumerate(REF_MARKERS)},
    **{m: f"参考视频 {i + 1}" for i, m in enumerate(REF_VIDEO_MARKERS)},
    **{m: f"参考音频 {i + 1}" for i, m in enumerate(REF_AUDIO_MARKERS)},
}

#: 入口分组，**按这个顺序显示**。手动对应表一次要摆三十个格子，不分组没人找得到。
#: 每组是 `(键, 组名, 这一组的标题)`。
MARKER_GROUPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "video",
        "画面入口",
        (
            "AIVS_PROMPT",
            "AIVS_NEGATIVE",
            "AIVS_FIRST_FRAME",
            "AIVS_LAST_FRAME",
            "AIVS_DURATION",
            "AIVS_SEED",
        ),
    ),
    ("ref_image", "参考图槽位", REF_MARKERS),
    ("ref_video", "参考视频槽位", REF_VIDEO_MARKERS),
    ("ref_audio", "参考音频槽位", REF_AUDIO_MARKERS),
    ("refine", "二次处理（超分 / 插帧）", ("AIVS_SOURCE_VIDEO",)),
    (
        "audio",
        "音源（另一条链，另一份图）",
        (
            "AIVS_AUDIO_TEXT",
            "AIVS_AUDIO_PROMPT",
            "AIVS_VOICE_REF",
            "AIVS_AUDIO_DURATION",
            "AIVS_AUDIO_SEED",
        ),
    ),
    ("image", "图片入口（出参考图那份图）", ("AIVS_WIDTH", "AIVS_HEIGHT")),
)

#: 少了这一个就没法生成（提示词填不进去）；其余入口缺了只是「那一项用图里原来的值」。
#: 参考素材槽位一个都没有也照样能生成——只是人物形象只能靠首帧带，容易跑偏。
#: **首帧槽位刻意不是必需的**：没有它时首帧会当参考图 1 送进去（`comfy_preset._refs`），
#: 严格首尾帧只有转场要用，所以只写进 `FLF_REQUIRED`。
REQUIRED = ("AIVS_PROMPT",)
FLF_REQUIRED = ("AIVS_PROMPT", "AIVS_FIRST_FRAME", "AIVS_LAST_FRAME")
#: 二次处理（超分 / 插帧 / 重做尾段）那份图必须有的入口：待处理的那一段视频。
#: 提示词**不是必需的**——超分图往往一个文本框都没有。
REFINE_REQUIRED = ("AIVS_SOURCE_VIDEO",)
#: 音源那份图必须有的入口：**至少要有一处告诉它「说什么 / 什么声音」**。
#: 写成「二者之一」而不是两个都要：TTS 图只要台词，环境音图只要一句描述。
AUDIO_REQUIRED_ANY = ("AIVS_AUDIO_TEXT", "AIVS_AUDIO_PROMPT")

HOW_TO = [
    "在 ComfyUI 里右键入口节点 → Title，改成 AIVS_PROMPT / AIVS_FIRST_FRAME 等",
    "想让角色表 / 地点参考图一起喂进去：把接参考图的节点标题改成 AIVS_REF_1、AIVS_REF_2…"
    f"（最多 {REF_SLOTS} 个，有几个标几个）",
    f"参考视频标 AIVS_REF_VIDEO_1…{REF_VIDEO_SLOTS}、参考音频标 AIVS_REF_AUDIO_1…"
    f"{REF_AUDIO_SLOTS}——它们接的是 LoadVideo / LoadAudio 那类节点，与图片槽位分开算",
    "只有补转场的那份图需要标全 AIVS_FIRST_FRAME + AIVS_LAST_FRAME；出正片的 R2V 图不标也能用",
    "二次处理（超分 / 插帧）那份图把接待处理视频的节点标成 AIVS_SOURCE_VIDEO"
    "——它和 AIVS_REF_VIDEO_n 不是一回事：源视频是「就处理这一段」，参考视频是「动作长这样」",
    "音源那份图另存一份：台词标 AIVS_AUDIO_TEXT、声音描述标 AIVS_AUDIO_PROMPT"
    "（两者有其一即可）、音色参考标 AIVS_VOICE_REF、时长与种子标 AIVS_AUDIO_DURATION /"
    " AIVS_AUDIO_SEED——它不需要 AIVS_PROMPT",
    "出参考图（角色四视图 / 地点图 / 道具图）那份 T2I 图也另存一份：提示词、负向、种子、"
    "参考图槽位用的是同一批标题，只多两个可选的 AIVS_WIDTH / AIVS_HEIGHT"
    "——它由设置页的「图片预设」指名，不靠标题猜",
    "再用「Save (API Format)」导出，重新上传这份预设",
]

NAME_OK = re.compile(r"^[\w一-鿿][\w一-鿿 .-]{0,63}$")


def presets_dir() -> Path:
    path = settings.runtime_dir / "presets"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _path_of(name: str) -> Path:
    if not NAME_OK.match(name or ""):
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "预设名不合法",
            f"{name!r} 含有不能作为文件名的字符。",
            ["用中英文、数字、空格、点、短横线", "例如 wan-i2v-快速"],
        )
    return presets_dir() / f"{name}.json"


def entry_points(graph: dict[str, Any]) -> dict[str, dict[str, str]]:
    """按标题找入口。返回 {标题: {node_id, field, class_type}}。"""
    found: dict[str, dict[str, str]] = {}
    for node_id, node in graph.items():
        title = str((node.get("_meta") or {}).get("title") or "").strip()
        candidates = MARKERS.get(title)
        if not candidates or title in found:
            continue
        inputs = node.get("inputs") or {}
        field = next((k for k in candidates if k in inputs), None)
        if field is None:
            raise AppError(
                ErrorCode.INVALID_WORKFLOW,
                f"入口节点 {title} 没有可填的输入",
                f"节点 {node_id}（{node.get('class_type')}）的输入是："
                f"{'、'.join(map(str, inputs)) or '（空）'}，"
                f"这里期望其中有 {'、'.join(candidates)} 之一。",
                [
                    "把标题挪到真正接收这个值的节点上（例如 LoadImage / CLIPTextEncode）",
                    *HOW_TO,
                ],
                {"node_id": node_id, "title": title},
            )
        found[title] = {
            "node_id": str(node_id),
            "field": field,
            "class_type": str(node.get("class_type") or ""),
        }
    return found


def ref_slots(points: dict[str, dict[str, str]], media: str = "image") -> list[str]:
    """这份图能收几个某一媒体的参考素材——按 `AIVS_REF_1`、`AIVS_REF_2`… 的序号排好。

    刻意按声明顺序（`REF_MARKERS_BY_MEDIA`）而不是字典顺序：账单里优先级最高的那个要进
    1 号槽，「第几个是谁」才对得上（`base.ref_hint` 拼给模型的那句说明也是这个顺序）。
    中间空一号（只标了 1 和 3）也不算错，就是两个槽位——我们不去猜用户为什么跳号。

    `media` 不认识时回空列表：那种素材这份图根本收不了，等于零个槽位，
    不该因为多了一种媒体就抛（问这句话的全是只读路径）。
    """
    return [m for m in REF_MARKERS_BY_MEDIA.get(media, ()) if m in points]


def ref_slots_by_media(points: dict[str, dict[str, str]]) -> dict[str, list[str]]:
    """三种媒体各有哪些槽位。适配器填槽位、账单算上限都从这一张表出发。"""
    return {media: ref_slots(points, media) for media in REF_MARKERS_BY_MEDIA}


#: 文件路径 → (mtime_ns, 字节数, {媒体: 槽位数})。「这份图能收几个」是一句会被反复问的话
#: （上下文账单、编排账单、界面上每一处都要问），一次解析一份几十万字节的图太贵。
#: key 里带上 mtime 与大小：文件一改缓存自然失效，所以这不是「可能过期的快照」，
#: 而是「同一份文件不重复解析」。
_slot_cache: dict[str, tuple[int, int, dict[str, int]]] = {}


def reset_cache() -> None:
    """清掉槽位数缓存。测试与 `registry.reset()` 用——预设目录会整体换掉。"""
    _slot_cache.clear()


def slot_counts(name: str) -> dict[str, int] | None:
    """这份预设三种媒体各标了几个槽位。数不出来时回 `None`（= 别拿它当上限）。

    数不出来有三种：没给名字、文件不在、文件坏了 / 缺必需入口。**一律不抛**——
    问这句话的地方全是只读路径（上下文账单、编排账单、界面），在那里因为预设坏了就
    500，人连「哪里坏了」都看不到；真正提交时 `submit()` 会拿同一份文件把话说清楚。

    回的是 `{"image": n, "video": n, "audio": n}`，三个键一定齐全（数出来是 0 也写 0）：
    调用方按媒体取上限，缺键就得到处写 `.get(media, 0)`。
    """
    if not name:
        return None
    try:
        path = _path_of(name)
        stat = path.stat()
    except (AppError, OSError):
        return None
    key = path.as_posix()
    hit = _slot_cache.get(key)
    if hit is not None and hit[0] == stat.st_mtime_ns and hit[1] == stat.st_size:
        return dict(hit[2])
    try:
        points = entry_points(load(name))
    except AppError:
        return None
    counts = {media: len(slots) for media, slots in ref_slots_by_media(points).items()}
    _slot_cache[key] = (stat.st_mtime_ns, stat.st_size, counts)
    return dict(counts)


def slot_count(name: str, media: str = "image") -> int | None:
    """这份预设标了几个某一媒体的槽位。默认问的是参考图（最常问的那一种）。"""
    counts = slot_counts(name)
    return None if counts is None else counts.get(media, 0)


def capacity_of(name: str) -> RefCapacity:
    """这份预设一次能收几个参考素材，**连那句人话一起给**。

    以前这段文案长在 `comfy_preset.ref_capacity()` 里，只会数**设置里那份默认预设**。
    但真正该数的是「这个工程这个能力最终会提交的那一份」——首尾帧镜头走 `flf_preset_name`、
    普通镜头走 `r2v_preset_name`，两份图标的槽位数完全可以不一样。所以把它下沉到这里：
    `services/route.py::capacity()` 解析出是哪一份之后拿名字来问，
    `comfy_preset.ref_capacity()` 则是「按当前设置会怎样」那一问，转调同一段话。

    三种媒体各回一个数：一份图标了 3 张图片槽 + 1 段音频槽是常见的事，折成一个数字的话
    账单只能说「还能再喂 1 个」，而用户塞进去的那一个大概是图。

    **绝不抛**（`slot_counts()` 那条同样的理由）：数不出来一律「不限制」，凭空造一个数字
    只会白丢用户的角色图 / 场景图，而这份图真正的问题会在提交那一刻说清楚。
    """
    counts = slot_counts(name)
    if counts is None:
        return RefCapacity(
            None,
            name,
            (
                f"读不到预设 {name}（文件不在或填不进去），这里先不限数量；"
                "真正生成时会先报出这份图的问题。"
                if name
                else "还没有选生成预设，这里先不限数量；真正生成时会报「还没有选生成预设」。"
            ),
        )
    image, video, audio = counts["image"], counts["video"], counts["audio"]
    if image == 0:
        detail = (
            f"预设 {name} 里一个 AIVS_REF_* 都没标——角色图 / 场景图全都喂不进去，"
            "人物形象只能靠首帧带。"
        )
    else:
        detail = f"预设 {name} 标了 {image} 个 AIVS_REF_* 槽位，一次最多喂 {image} 张参考图。"
    extra = "、".join(
        f"{MEDIA_LABEL[media]} {n} 个" for media, n in (("video", video), ("audio", audio)) if n
    )
    if extra:
        detail += f"另外还能收 {extra}。"
    return RefCapacity(image, name, detail, video=video, audio=audio)


def _media_tail(by_media: dict[str, list[str]]) -> str:
    """参考视频 / 参考音频那半句。一个都没标就什么都不说——绝大多数图只收图片，
    给每份预设都挂一句「没有参考视频槽位」只会把真正的问题埋掉。"""
    parts = [
        f"{len(by_media.get(media) or [])} 个{MEDIA_LABEL[media]}"
        for media in ("video", "audio")
        if by_media.get(media)
    ]
    return f"；另外还能收 {'、'.join(parts)}" if parts else ""


def _ref_hint(by_media: dict[str, list[str]], first_frame: bool) -> str:
    """这份图怎么收素材——UI 上那一句话。四种情况分开说，别只说「有几个参考图槽位」。

    图片那半句是主语（首帧的降级只跟它有关），视频 / 音频只在真标了槽位时补一句：
    参考图 0 槽是需要提醒的事，参考视频 0 槽是常态。
    """
    slots = by_media.get("image") or []
    tail = _media_tail(by_media)
    if not first_frame:
        if not slots:
            return (
                "这份图收不进任何图：首帧与角色表 / 地点参考图都喂不进去，只有提示词起作用。"
                f"要收图就加 AIVS_FIRST_FRAME 或 AIVS_REF_1…AIVS_REF_{REF_SLOTS} 标题"
            ) + tail
        return (
            f"没有首帧槽位：首帧会当参考图 1 送进去，这份图一共收 {len(slots)} 张"
            f"（{'、'.join(slots)}）。补转场要用的严格首尾帧请另存一份标了"
            "AIVS_FIRST_FRAME + AIVS_LAST_FRAME 的预设"
        ) + tail
    if slots:
        return f"能收 {len(slots)} 张参考图（{'、'.join(slots)}）{tail}"
    return (
        "没有参考图槽位：角色表 / 地点参考图喂不进去，人物形象只能靠首帧带。"
        f"要支持就在图里加 AIVS_REF_1…AIVS_REF_{REF_SLOTS} 标题"
    ) + tail


def inspect(graph: dict[str, Any]) -> dict[str, Any]:
    """预设的体检报告：找到哪些入口、缺哪些、缺了会怎样。

    **一份图能做的事不止出画面**（`capabilities`）：出正片（r2v）、补转场（flf）、
    二次处理（refine）、出声音（audio）。所以 `ready` 是「至少能做一件事」，
    而不是「有 AIVS_PROMPT」——音源图与超分图压根不需要画面提示词，按老口径它们连保存
    都过不了，用户只能被逼着往音源图里塞一个没人读的文本框。
    """
    points = entry_points(graph)
    missing = [m for m in REQUIRED if m not in points]
    missing_flf = [m for m in FLF_REQUIRED if m not in points]
    by_media = ref_slots_by_media(points)
    slots = by_media["image"]
    first_frame = "AIVS_FIRST_FRAME" in points
    refine_ready = all(m in points for m in REFINE_REQUIRED)
    audio_ready = any(m in points for m in AUDIO_REQUIRED_ANY)
    ready = not missing or refine_ready or audio_ready
    return {
        "node_count": len(graph),
        "entry_points": points,
        "found": sorted(points),
        "missing_required": missing,
        "ready": ready,
        "r2v_ready": not missing,
        "flf_ready": not missing_flf,
        #: 能不能当二次处理 / 音源那份图用。两者各自独立，与出画面互不影响：
        #: 同一份图既能出正片又能超分是可能的（标了 AIVS_SOURCE_VIDEO 就行）。
        "refine_ready": refine_ready,
        "audio_ready": audio_ready,
        #: 有没有首帧入口。没有不影响 ready，但首帧只能当参考图送——UI 要标出来。
        "first_frame_ok": first_frame,
        "capabilities": [
            capability
            for capability, available in (
                ("r2v", not missing),
                ("flf", not missing_flf),
                #: 二次处理与出声音各算一项独立能力：设置页要能一眼看出「哪一份是音源图」。
                ("refine", refine_ready),
                ("audio", audio_ready),
                #: 能收参考视频 / 参考音频算两项独立能力：动作参考与对白音频是两类图，
                #: UI 上要能一眼看出「这份图接不接音频」。
                ("ref_video", bool(by_media["video"])),
                ("ref_audio", bool(by_media["audio"])),
            )
            if available
        ],
        #: 能收几张参考图。0 不影响 ready——只是这份图喂不进角色表，UI 要提醒。
        "ref_slots": len(slots),
        #: 参考视频 / 参考音频的槽位数。与 `ref_slots` 分开给：混成一个数会让界面
        #: 显示「能收 5 个参考素材」而其中 2 个只吃音频，用户照着塞图必然白跑一趟。
        "ref_video_slots": len(by_media["video"]),
        "ref_audio_slots": len(by_media["audio"]),
        "ref_slots_by_media": {media: len(v) for media, v in by_media.items()},
        "ref_hint": _ref_hint(by_media, first_frame),
        "impact": (
            None
            if ready
            else "这份图里既没有 AIVS_PROMPT（出画面要它），也没有 AIVS_SOURCE_VIDEO"
            "（二次处理要它）或 AIVS_AUDIO_TEXT / AIVS_AUDIO_PROMPT（出声音要它）"
            "——本工具无法往里填任何东西。"
        ),
    }


def save(name: str, raw: str) -> dict[str, Any]:
    """存一份预设。存之前先体检，绝不把一份填不进去的图悄悄留下。"""
    from app.services.workflows import parse_graph  # 延迟导入：避免生成层反向依赖 service 层

    graph = parse_graph(raw)
    report = inspect(graph)
    if not report["ready"]:
        raise AppError(
            ErrorCode.INVALID_WORKFLOW,
            "这份图里找不到必需的入口",
            str(report["impact"]),
            HOW_TO,
            {"found": report["found"], "missing": report["missing_required"]},
        )
    target = _path_of(name)
    tmp = target.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(target)
    except OSError as exc:
        raise AppError(
            ErrorCode.DISK_FULL if exc.errno == 28 else ErrorCode.VALIDATION_ERROR,
            "预设写入失败",
            f"{target}: {type(exc).__name__}: {exc}",
            ["确认磁盘可写且空间充足"],
        ) from exc
    return {"name": name, "path": target.as_posix(), **report}


def load(name: str) -> dict[str, Any]:
    from app.services.workflows import parse_graph

    target = _path_of(name)
    if not target.is_file():
        raise AppError(
            ErrorCode.NOT_FOUND,
            "预设不存在",
            f"{target.name} 不在 {presets_dir().as_posix()} 里。",
            ["在设置页上传这份图的 API 格式 json", "或改选一个已有的预设"],
            {"name": name},
        )
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "预设读不出来",
            f"{target}: {type(exc).__name__}: {exc}",
            ["重新上传这份预设"],
        ) from exc
    return parse_graph(raw)


def listing() -> list[dict[str, Any]]:
    """设置页的预设列表。坏文件不隐藏——标成 ready=false 并写清原因。"""
    rows: list[dict[str, Any]] = []
    for path in sorted(presets_dir().glob("*.json")):
        # 坏文件也给全 UI 要用的键：形状不稳会让列表少画一块，而不是显示「这份图坏了」
        item: dict[str, Any] = {
            "name": path.stem,
            "path": path.as_posix(),
            "ready": False,
            "r2v_ready": False,
            "flf_ready": False,
            "refine_ready": False,
            "audio_ready": False,
            "first_frame_ok": False,
            "capabilities": [],
            "ref_slots": 0,
            "ref_video_slots": 0,
            "ref_audio_slots": 0,
            "ref_slots_by_media": {"image": 0, "video": 0, "audio": 0},
            "ref_hint": "",
        }
        try:
            item.update(inspect(load(path.stem)))
        except AppError as err:
            item["impact"] = f"{err.title}：{err.detail}"
        rows.append(item)
    return rows


def delete(name: str) -> None:
    _path_of(name).unlink(missing_ok=True)

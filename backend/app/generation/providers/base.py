"""视频生成适配层：与模型无关的形状。

生成层不再由本工具维护 ComfyUI 的图。这里只定义「一次 R2V 请求长什么样」与
「一个服务要能做哪四件事」，具体差异全部关在同目录的适配器里——
service 层永远不出现 `if provider == "xxx"`。

本轮只有 R2V（图 → 视频）：
  · `i2v` 只给首帧；
  · `flf` 给首尾帧（两幕之间那段 1~2s 转场就是它）。
T2V 暂不做——没有首帧的镜头在编排时就会被账单挡下来，而不是生成出一段跑偏的画面。

**首尾帧和参考素材不是一回事**，所以是两个字段：首尾帧决定「画面从哪一格开始 / 结束」，
参考素材决定「谁出场、长什么样、在哪儿、动作什么样、跟着哪段声音」。只喂一张首帧时最容易
丢的就是人物形象——账单里算出来的角色表 / 地点参考图必须能一起送到模型端，
这就是 `refs` 存在的理由。

**参考素材分三种媒体**（`RefAsset.media` = `image` / `video` / `audio`），因为模型端接它们
的节点根本不是一类：图片进 LoadImage 那类、视频进 VHS / LoadVideo 那类、音频进 LoadAudio
那类。混着数会把一段 `.mp4` 填进 LoadImage——既不报错也出不了片。所以**上限也按媒体分开**
（`RefCapacity.limit_of(media)`）：一份图标了 3 张图片槽 + 1 段音频槽是很常见的事，
折成一个「4 个参考素材」的数字，用户照着塞 4 张图必然白跑一趟。

**能收几个参考素材由适配器回答**（`RefCapacity` + `ref_capacity()`），不是应用级设置：
真实上限写在模型端那份图里（`comfy_preset` 数 `AIVS_REF_*` / `AIVS_REF_VIDEO_*` /
`AIVS_REF_AUDIO_*` 槽位），我们这边配一个数字只会和它打架。没有一份可数的图时就是
「不限制」，不凭空造上限。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

MODES = ("i2v", "flf", "refine")

#: 任务状态的统一口径，与 Job.status 对齐，适配器负责把各家的说法翻译成这四个。
STATUSES = ("queued", "running", "done", "failed")

#: 参考素材的三种媒体。**这一串的顺序就是对外展示的顺序**（账单、降级说明、提示词里那句
#: 「参考图1=…」都按它排），别在别处再定一次。
MEDIA = ("image", "video", "audio")

#: 媒体的中文说法。与 `presets.MEDIA_LABEL` 是同一份口径，这里再列一遍是因为
#: `base` 不该反向依赖某一个适配器的模块。
MEDIA_LABEL = {"image": "参考图", "video": "参考视频", "audio": "参考音频"}

#: 一条素材说明最多带多少字进 prompt。**截断规则只有这一处**（`clip_desc`，三个调用点：
#: 图册那一行 `subject_definitions`、`ref_hint` 里编不上号的那几个、REST 合同的 `refs[].desc`）：
#: 素材描述是自由文本，用户可以写一整段设定，几条加起来就能把正向 prompt 顶掉；
#: 而截断在两处各写一遍的话，界面上提示的字数与真正送出去的必然分叉。
#: 前端从 `GET /projects/{pid}/assets/undescribed` 的账单里读这个数，不写死第二份。
DESC_MAX = 120


def clip_desc(text: str, limit: int = DESC_MAX) -> str:
    """素材说明进 prompt 之前的唯一处理：去空白 + 压掉换行 + 超长截断。

    压换行是因为这句话会被拼进一行提示词里，用户在文本框里敲的回车不该变成 prompt 的结构。
    """
    one = " ".join(str(text or "").split())
    return one if len(one) <= limit else f"{one[:limit]}…"


@dataclass(frozen=True, slots=True)
class RefAsset:
    """一个参考素材：文件在哪 + 它是谁 + 它是什么媒体。

    `label` / `kind` 直接来自上下文账单（角色表 / 地点参考 / 道具参考 / 手动添加）。模型端
    接不接收这句说明由适配器决定——但**不许因为带不了标签就把它丢掉**。

    `media` 决定它进哪一组槽位，来源是文件后缀（`assets.kind_of_suffix`），不是用户手填：
    真正决定「这个文件能不能填进 LoadImage」的是它到底是什么文件。

    `desc` 是这张素材**长什么样**（`Asset.description`，用户手填或 AI 看图补的那一句），
    与 `label` 分开是刻意的：`label` 要短，它还要显示在上下文检查器、`dropped_labels`
    与底部控制台里；`desc` 只服务于提示词，由 `clip_desc()` 截断后单独渲染
    （图册里那一行 `subject_definitions`，或 REST 合同的 `refs[].desc`）。
    空 = 用户没写，此时那一行只剩一个名字。

    `name` 是**只有名字**的那一份（`阿岚（默认形象）` / `阴曹试院 · 廊下考场`），空 = 没给。
    它与 `label` 分开是因为 `label` 里带着台账字样（`· Character Sheet v1`、`（本幕人物）`
    这类给人看的来源标注）——那些字进了 prompt 就是噪声，而模型要靠这个名字把
    `overall_soundscape` 里那句「宋焘说：…」对上 `<Subject 1>`（见 `render_video_prompt`）。
    """

    path: Path
    label: str = ""
    kind: str = ""
    media: str = "image"
    desc: str = ""
    name: str = ""

    @property
    def media_label(self) -> str:
        return MEDIA_LABEL.get(self.media, "参考素材")

    @property
    def who(self) -> str:
        """进提示词的那个名字：干净的名字 → 台账标签 → 文件名。**只有这一处口径。**"""
        return self.name or self.label or self.path.name


#: 旧名字。参考素材支持视频 / 音频之前它只可能是图，改名后留一个别名给外部引用
#: （`tests/test_providers.py` 那类只关心「有个参考素材形状」的地方）。
RefImage = RefAsset


@dataclass(frozen=True, slots=True)
class RefCapacity:
    """这条生成路径一次能收几个参考素材（**首尾帧不算在内**）。

    「最多喂几个」不是本工具的偏好，而是模型端那份图的事实，所以它由适配器回答，
    不再是应用级设置——设置里那个数字只会和真实槽位数打架，还得用户自己去对。

    `limit is None` = **不限制**：这条路上没有一份可数的图（通用 REST 合同天生收多个，
    旧的绑定路径压根不注入素材），此时凭空造一个上限只会白丢用户的素材。
    `limit == 0` 是一个有意义的答案，不是「没查到」：那份图一个 `AIVS_REF_*` 都没标，
    角色表 / 地点图全都进不去——这正是人物形象跑偏的现场，必须说出来。

    `limit` / `dropped()` 说的**只是图片**（问得最多的那一种，也是历史上唯一一种）；
    视频 / 音频各有自己的数字，走 `video` / `audio` 或 `limit_of(media)` 取——
    三种媒体折成一个数字的话，「还能再喂 1 个」到底指图还是音频就说不清了。

    `source` 是这个数字从哪来的（预设名 / 合同），`detail` 是给人看的一句话，
    两个都会一路传到界面上：「预设只有 3 槽」和「这条路不限张数」的处置方式完全不同。
    """

    limit: int | None = None
    source: str = ""
    detail: str = ""
    #: 参考视频 / 参考音频的上限，含义与 `limit` 完全一致（`None` = 不限制）。
    video: int | None = None
    audio: int | None = None

    def limit_of(self, media: str) -> int | None:
        """某一媒体的上限。不认识的媒体回 0——那种素材这条路根本收不了。"""
        if media == "image":
            return self.limit
        if media == "video":
            return self.video
        if media == "audio":
            return self.audio
        return 0

    def dropped(self, count: int) -> int:
        """账单给了 `count` 张**图片**时，会有几张喂不进去。"""
        return self.dropped_of("image", count)

    def dropped_of(self, media: str, count: int) -> int:
        """账单给了 `count` 个某一媒体的素材时，会有几个喂不进去。"""
        limit = self.limit_of(media)
        if limit is None:
            return 0
        return max(0, count - limit)


@dataclass(frozen=True)
class WorkflowSpec:
    """用户自己那份 ComfyUI 图 + 绑定表（工作流绑定那条路专用）。

    **刻意不塞进 `extra`**：`extra` 会被 service 层原样冻结进 `params_json`，一份 api_json
    动辄几十 KB，每个版本存一遍会把工程库撑起来。这里只在提交那一刻传给适配器，
    冻结进版本参数的是 `workflow_id`（哪一份图），需要复现时按 id 取。

    适配器**不认识**图里的 lora 与加速节点：`bindings` 说「哪个节点的哪个字段收首帧」，
    其余一律原样提交（硬约束 1）。
    """

    id: str
    name: str
    api_json: str
    bindings: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class VideoRequest:
    """一次生成请求。`extra` 原样透传给模型端，本工具不解释里面的东西。"""

    mode: str
    prompt: str = ""
    negative: str = ""
    first_frame: Path | None = None
    last_frame: Path | None = None
    #: 首尾帧之外的参考素材（图片 / 视频 / 音频混在一个列表里，按账单顺序、优先级高的在前）。
    #: 刻意**不按媒体分成三个字段**：账单里的优先级是跨媒体排的，拆开就得在适配器里
    #: 重新合并一次顺序；分组是填槽位那一步的事（`comfy_preset._refs`）。
    refs: list[RefAsset] = field(default_factory=list)
    duration: float = 4.0
    seed: int | None = None
    #: **二次处理的输入**：已经出好的那一段视频（`mode="refine"`）。与 `refs` 里的参考视频
    #: 严格分开——源视频是「就处理这一段」，参考视频是「动作长这样」。混用的话超分图会把
    #: 一段参考视频当成待处理画面，出来的东西跟这个镜头无关，而界面上会显示「已生成」。
    source_video: Path | None = None
    #: 工作流绑定那条路要提交的那份图（其余适配器忽略它）。装配条件是「这个任务绑了图」，
    #: 不是「`if provider == ...`」——业务层不许认路（硬约束 1）。
    workflow: WorkflowSpec | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    #: 适配器提交时写下的降级说明，例如「这份图只有 3 个参考图槽位，账单里第 4 张没喂进去」。
    #: service 层原样冻结进版本，不解释内容——「绝不静默失败」在这里的样子是
    #: 「降级也要说出来并留档」，而不是抛错让整个任务失败。
    notes: list[str] = field(default_factory=list)
    #: `prompt` 拆回来的那几段（`camera_motion` / `visual_prompt` / `audio_dialogue`）。
    #: **由 service 层拆好传下来**（`ai/prompts.py::shot_segments`，全应用只有那一个解析器）：
    #: 适配层不 import `app.ai`，自己再解析一遍必然与拼 prompt 的那一处分叉。
    #: 空 dict = 这条 prompt 不是四段格式（用户手写的自由文本），此时整段原样进
    #: `detailed_description`（见 `render_video_prompt`）。
    segments: dict[str, str] = field(default_factory=dict)
    #: 这是第几个镜头（`[SHOT n]` 里那个 n）。入队参数里没有 `index_no`，所以它同样由
    #: service 层从 prompt 上解析（`prompts.shot_no_of`）。渲染成 `[Shot n]`。
    shot_no: int = 1
    #: **真正提交出去的那段正向 prompt**（六段格式的全文）。适配器填，service 层冻结进
    #: `params.prompt_sent`：入队时冻结的 `params.prompt` 是拼装前的四段格式，
    #: 「当时到底喂了哪段话」以前在任何地方都查不到（硬约束 4）。
    sent_prompt: str = ""
    #: 这一次的图册（`<Picture n>` / `<Subject n>` 到底指谁）。适配器填，service 层冻结进
    #: `params.pictures`。`None` = 这条路不编号（通用 REST 那类收得到结构化字段的端）。
    book: PictureBook | None = None


def refs_by_media(refs: Sequence[RefAsset]) -> dict[str, list[RefAsset]]:
    """把参考素材按媒体分组，组内保持账单顺序。三个键一定齐全（空组给空列表）。

    分组这件事**只在这里做一次**：适配器填槽位、账单算上限、降级说明分媒体说，
    各写一份 `if media == ...` 迟早在「第几个是谁」上分叉。
    """
    groups: dict[str, list[RefAsset]] = {media: [] for media in MEDIA}
    for ref in refs:
        groups.setdefault(ref.media, []).append(ref)
    return groups


def ref_hint(refs: Sequence[RefAsset]) -> str:
    """把「第几个参考素材是谁」写成一句话。**现在只服务于编不进 `<Picture n>` 的那些。**

    图片走图册（`picture_book` → `render_video_prompt` 的 `subject_definitions`）：那条路的
    编号是按这份图的接线实测出来的，而这里的序号只是「账单里的第几个」——两者对不上时，
    照这句话认人会让模型把角色互相串味，这正是当初那个 bug 的形状。
    参考视频 / 参考音频没有 `<Picture n>` 可给（模型收到的那一串编号说的是图片），
    所以它们仍然只能靠这句话点名，`render_video_prompt` 在 `summary` 里引它。

    序号**按媒体各自从 1 数**，因为槽位就是按媒体分开的：视频进 `AIVS_REF_VIDEO_1`、
    音频进 `AIVS_REF_AUDIO_1`，混在一起连续编号的话这句说明会和真正填进去的槽位错位。

    有描述的素材多一个括号：`参考音频1=对白（三十岁男声，压低）`。
    **没有描述时输出与升级前逐字相同**——老工程的 prompt 不该因为多了一列而变样。
    """
    parts: list[str] = []
    for media, group in refs_by_media(refs).items():
        label = MEDIA_LABEL.get(media, "参考素材")
        for i, r in enumerate(group, 1):
            desc = clip_desc(r.desc)
            who = r.who
            parts.append(f"{label}{i}={who}（{desc}）" if desc else f"{label}{i}={who}")
    if not parts:
        return ""
    return f"参考素材说明：{'；'.join(parts)}。"


#: 图册里那两个「决定画面第一 / 最后一格」的角色（角色名来自上下文账单
#: `services/context.py::_assign_roles`）。其余角色说的是「谁出场、长什么样」，
#: 也就是会变成一个 `<Subject n>` 的那些。
FRAME_ROLES = frozenset({"first_frame", "last_frame"})

#: 那两张帧在图册里给人看的说法。各条路的入口名不一样（预设是 `AIVS_FIRST_FRAME`、绑定是
#: `first_frame`），但**落到图册上的这两个名字只有这一份**——它会进 `params.pictures`
#: 与界面上那张对号表，两处写成不同的字会让人以为是两回事。
FRAME_LABEL = {"first_frame": "首帧", "last_frame": "末帧"}

#: 「这张图定义的是什么」→ 定义句里那半句英文。**措辞只有这一份表**，键是账单里的 `kind`。
_DEFINES = {
    "character_sheet": "the visible subject shown in",
    "appearance": "the visible subject shown in",
    "location_reference": "the environment shown in",
    "prop_reference": "the object shown in",
}
_DEFINES_ELSE = "the visible subject shown in"

#: 「这张图必须保住什么」→ `retention_analysis` 里那半句。同上，只有这一份。
_RETAIN = {
    "character_sheet": "keep the exact face, hair, build and costume shown in",
    "appearance": "keep the exact face, hair, build and costume shown in",
    "location_reference": "keep the exact layout, architecture, materials and lighting shown in",
    "prop_reference": "keep the exact shape, material, colour and markings shown in",
}
_RETAIN_ELSE = "keep the exact appearance shown in"

#: 「这张图上是个人」的那几种 kind：只有它们之间需要那句「这是两个不同的人，别互相串」。
_PEOPLE = frozenset({"character_sheet", "appearance"})


@dataclass(frozen=True, slots=True)
class Picture:
    """图册里的一张图：**这一次它是第几张**、是谁、从哪个入口喂进去的。

    `index` 是 `<Picture n>` 里那个 n，**1 起、把首尾帧一起数进去**——模型看到的就是一串
    图片，首帧不会因为我们在代码里分了两个字段就自动排到编号之外。顺序来自
    `comfy/graph.py::feed_order()` 实测的接线，不是 `AIVS_REF_*` 的标题序号。

    `subject` 是 `<Subject n>` 里那个 n，**只有非首尾帧的那些图才有**（首尾帧说的是「画面
    从哪一格开始」，不是「谁出场」）：0 = 这张图不定义任何 subject。
    """

    index: int = 0
    role: str = "reference"
    kind: str = ""
    media: str = "image"
    name: str = ""
    desc: str = ""
    #: 喂它的那个入口名（`AIVS_REF_2` / `first_frame` / `__ref_0`）。排查时要的就是这一格。
    entry: str = ""
    #: 上传到模型端之后的文件名。**去重按它**（见 `picture_book`）。
    file: str = ""
    subject: int = 0

    @property
    def tag(self) -> str:
        return f"<Picture {self.index}>"

    @property
    def subject_tag(self) -> str:
        return f"<Subject {self.subject}>"

    @property
    def is_frame(self) -> bool:
        return self.role in FRAME_ROLES

    @property
    def is_person(self) -> bool:
        return self.kind in _PEOPLE

    def to_dict(self) -> dict[str, Any]:
        """冻结进 `params.pictures`、也显示在界面上的那一行。"""
        return {
            "index": self.index,
            "subject": self.subject,
            "role": self.role,
            "kind": self.kind,
            "media": self.media,
            "name": self.name,
            "desc": self.desc,
            "entry": self.entry,
            "file": self.file,
        }


@dataclass(frozen=True)
class PictureBook:
    """这一次提交的图册：`<Picture n>` / `<Subject n>` 分别指谁。

    **它是提示词与那几张图之间唯一的对号表**：`render_video_prompt()` 照它写那六段，
    service 层照它冻结 `params.pictures`，界面照它显示「这一版的第 3 张图是张秀才」。
    两处各编一遍号必然分叉，而分叉的样子恰好是「模型把两个角色画成同一个人」。
    """

    items: list[Picture] = field(default_factory=list)
    #: 编不进号的那些参考素材（参考视频 / 参考音频）：ComfyUI 那类图收不到标签，
    #: 只能在 `summary` 里用 `ref_hint()` 那句话点一下，没有 `<Picture n>` 可给。
    others: list[RefAsset] = field(default_factory=list)
    #: 编号顺序是怎么来的（`graph` / `mixed` / `title`，见 `comfy/graph.py::FeedOrder`；
    #: 另有 `contract`——收得到结构化字段的那条路由我们定顺序，没有图可测也无需降级说明）。
    order_source: str = "title"
    #: 说明与降级（顺序是实测的还是退回了约定）。调用方并进 `req.notes` → 冻结 → 界面。
    notes: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        """一张图都没有时为假——调用方照此决定要不要走六段格式。"""
        return bool(self.items)

    @property
    def subjects(self) -> list[Picture]:
        """会变成 `<Subject n>` 的那几张（首尾帧不算）。"""
        return [p for p in self.items if p.subject]

    @property
    def listing(self) -> str:
        """`<Picture 1>=宋焘；<Picture 2>=张秀才`——给人看的对号表。

        note、日志、界面共用这一句。**它不是提示词的一部分**：模型收到的对号表是
        `subject_definitions` 那一段（`render_video_prompt`），两处措辞刻意不同——
        一处给人排查用，一处要让模型认得住。
        """
        return _listing(self.items)

    def of_role(self, role: str) -> Picture | None:
        return next((p for p in self.items if p.role == role), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_source": self.order_source,
            "items": [p.to_dict() for p in self.items],
            "others": [
                {"name": r.who, "media": r.media, "kind": r.kind, "file": r.path.name}
                for r in self.others
            ],
            "notes": list(self.notes),
        }


def frame_seed(role: str, file: str) -> Picture:
    """首帧 / 末帧那一张的图册种子。**三条路共用这一处**（措辞见 `FRAME_LABEL`）。

    它照旧占一个 `<Picture n>` 号：模型看到的是一串图片，首尾帧不会因为我们在代码里分成了
    两个字段就自动排到编号之外。以前几份 SKILL 写死「参考图 1 是首帧」，而首帧到底排第几
    完全取决于这份图怎么接的线——那正是「`<Picture 1>` 指错人」的来源。
    """
    return Picture(
        role=role, kind=role, media="image", name=FRAME_LABEL.get(role, role), file=str(file)
    )


def picture_book(
    seeds: Mapping[str, Picture],
    order: Sequence[str] = (),
    *,
    order_source: str = "title",
    others: Sequence[RefAsset] = (),
) -> PictureBook:
    """把「入口 → 这张图是谁」按**实际喂入顺序**编成图册。**编号口径只有这一处。**

    `seeds` 的键是入口名（`AIVS_FIRST_FRAME` / `AIVS_REF_2` / `__ref_0`…），**它的顺序就是
    调用方的约定顺序**（标题序号 / 绑定表行号）；`order` 是 `feed_order()` 从接线上实测出来的
    那一串，只用来定序、不做筛选——不在 `order` 里的入口照旧按约定顺序排在后面。

    **按文件去重**：同一个文件出现在两个入口上是常态（绑定那条路的「参考图（单槽）」与
    `__ref_0` 常常指着同一个 LoadImage，`reconnect()` 还会把同一张图接给必填的那一格），
    编成两个号就等于告诉模型「这是两张不同的图」。
    """
    keys = [key for key in order if key in seeds]
    keys += [key for key in seeds if key not in keys]
    items: list[Picture] = []
    kept: list[str] = []
    seen: set[str] = set()
    index = 0
    subject = 0
    for key in keys:
        seed = seeds[key]
        #: 非图片不编号（模型看到的那一串 `<Picture n>` 说的就是图）。
        if seed.media != "image":
            continue
        mark = seed.file or key
        if mark in seen:
            continue
        seen.add(mark)
        kept.append(key)
        index += 1
        frame = seed.role in FRAME_ROLES
        if not frame:
            subject += 1
        items.append(
            replace(seed, index=index, subject=0 if frame else subject, entry=seed.entry or key)
        )
    notes = _book_notes(items, kept, seeds, order_source)
    return PictureBook(items, list(others), order_source, notes)


def _listing(items: Sequence[Picture]) -> str:
    """图册的对号表（`PictureBook.listing` 与 `_book_notes` 共用，两处不各写一遍）。"""
    return "；".join(f"{p.tag}={p.name or p.file or p.entry}" for p in items)


def _book_notes(
    items: Sequence[Picture],
    kept: Sequence[str],
    seeds: Mapping[str, Picture],
    order_source: str,
) -> list[str]:
    """这一份编号是怎么来的——**与约定顺序不一致时必须说出来**（硬约束 4）。

    这条 note 一路进 `req.notes` → 版本参数 `ref_notes` → 界面。它是这次改造里最要紧的一句话：
    图里两根线接反了的时候，用户在 ComfyUI 界面上看不出来，而喂给模型的「`<Picture 1>` 是宋焘」
    会指错人——画面里两个角色互相串味，队列里一条错误都没有。
    """
    if len(items) < 2:
        return []
    listing = _listing(items)
    if order_source == "title":
        return [
            "这份图里这几个媒体入口没有汇合到同一个节点，`<Picture n>` 的编号只能按入口名的"
            f"顺序排（不是从接线上测出来的）：{listing}。"
        ]
    if list(kept) != [key for key in seeds if key in set(kept)]:
        return [
            "按这份图的接线，实际喂入顺序与入口名的顺序不一致，图册按**实测**的这一份编号："
            f"{listing}。"
        ]
    return []


def render_video_prompt(req: VideoRequest, book: PictureBook) -> str:
    """把这次要提交的正向 prompt 渲染成**参考生成那套六段格式**。全应用只有这一处拼装。

    **为什么不是「四段格式 + 末尾一句参考素材说明」**（老的 `ref_hint` 那条路）：那句说明挤在
    prompt 末尾时，模型读到的仍然是一段散文，`<Picture n>` 与画面里的人没有任何显式绑定；
    而分镜里「保持两人服饰与面容一致」这类话在缺少 `retention_analysis` 的形状下会被字面执行成
    「把这两个人画成同一个人」——那正是「张秀才长成了宋焘」的形状。六段格式给每张图一个
    `<Subject n>`、给每个 subject 一句「保住它自己、别与别人混」，这两句话在四段格式里
    压根没有地方落。

    **段名与 `<Subject n>` / `<Picture n>` 一律英文**（模型端认的是这套结构），段里的内容照
    原文的语言写——分镜是中文的，在这里翻译一遍只会丢细节。

    `req.segments` 是空的（用户手写的自由文本 prompt）时不硬套三段，整段原样进
    `detailed_description`：不替用户重写他自己写的 prompt。
    """
    shot = f"[Shot {max(1, int(req.shot_no or 1))}]"
    seconds = f"{max(0.0, float(req.duration or 0)):.2f}"
    parts: list[str] = []
    align = _align_line(book, shot, seconds)
    if align:
        parts.append(align)
    parts.append(f"subject_definitions:\n{_definitions(book)}")
    parts.append(f"summary:\n{_summary(book, shot, seconds)}")
    parts.append(f"retention_analysis:\n{_retention(book, shot)}")
    parts.append(f"detailed_description:\n{_detail(req, shot)}")
    parts.append(f"overall_soundscape:\n{_soundscape(req)}")
    #: 无配乐是产品级硬约束（`ai/skills/video_prompt.py::AUDIO_RULE`），这一格永远是 none。
    parts.append("non_diegetic_music:\nnone")
    return "\n\n".join(parts)


def _and_join(parts: Sequence[str]) -> str:
    """`A` / `A and B` / `A, B and C`。这几段是英文句子，中文顿号会被读成一个词。"""
    items = [p for p in parts if p]
    if len(items) < 2:
        return items[0] if items else ""
    return f"{', '.join(items[:-1])} and {items[-1]}"


def _align_line(book: PictureBook, shot: str, seconds: str) -> str:
    """首尾帧与目标视频的对齐句（措辞照 `ai/skills/video_prompt.py` 那三份 SKILL 的第一行）。

    **由图册里真有哪几张帧决定，不由 SKILL 名字决定**：适配器看不到 SKILL 名，而「这一次到底
    喂了首帧还是末帧」它知道得最准。两处各说一遍的话，图册说两张、这句话说一张。
    """
    first = book.of_role("first_frame")
    last = book.of_role("last_frame")
    head = "How the reference pictures align with the target video — "
    if first is not None and last is not None:
        return (
            f"{head}{first.tag} (from {shot}) aligns with the 0.00-second mark of the target "
            f"video; {last.tag} (from {shot}) aligns with the {seconds}-second mark of the "
            "target video."
        )
    if first is not None:
        return (
            f"For the target video, at 0.00 seconds into the target video, {first.tag} "
            f"(from {shot}) is fully referenced."
        )
    if last is not None:
        return (
            f"{head}{last.tag} (from {shot}) aligns with the {seconds}-second mark of the "
            "target video."
        )
    return ""


def _definitions(book: PictureBook) -> str:
    """`<Subject n> is … shown in <Picture n>: 名字。它长什么样`。

    **名字必须进这一句**：`overall_soundscape` 里写的是「宋焘说：…」，模型要靠这里把那个名字
    接到 `<Subject n>` 上。缺了它，台词落到谁头上全靠猜——「张三说了李四的台词」就是这么来的。
    描述截断照旧只走 `clip_desc()`（`DESC_MAX`），图册与冻结参数里留的是全文。
    """
    lines = [
        f"{p.subject_tag} is {_DEFINES.get(p.kind, _DEFINES_ELSE)} {p.tag}: "
        + ("。".join(x for x in (p.name, clip_desc(p.desc)) if x) or p.file)
        for p in book.subjects
    ]
    #: 一个 subject 都没有时写 none（照 `_REF` 那份 SKILL 的规定），不留一个空段。
    return "\n".join(lines) or "none"


def _summary(book: PictureBook, shot: str, seconds: str) -> str:
    """`[reference generation] …`：这一次要出多长、有谁、哪几张图定义了他们、哪张是首尾帧。"""
    subjects = book.subjects
    line = f"[reference generation] Create a {seconds}-second target video"
    who = _and_join([p.subject_tag for p in subjects])
    line += f" featuring {who}." if who else "."
    if subjects:
        many = len(subjects) > 1
        line += (
            f" {_and_join([p.tag for p in subjects])} {'provide' if many else 'provides'} "
            f"the visible identity of {'these subjects' if many else 'the subject'}."
        )
    first = book.of_role("first_frame")
    last = book.of_role("last_frame")
    if first is not None and last is not None:
        line += f" {first.tag} is the first frame and {last.tag} is the final frame."
    elif first is not None:
        line += f" {first.tag} is the first frame of the target video."
    elif last is not None:
        line += f" {last.tag} is the final frame of the target video."
    #: 参考视频 / 参考音频编不进 `<Picture n>`，只能靠这句话点一下它们是谁
    #: （措辞借 `ref_hint()`，两处各写一遍必然分叉）。
    hint = ref_hint(book.others)
    return f"{line} {hint}" if hint else line


def _retention(book: PictureBook, shot: str) -> str:
    """每个 subject 一句「保住它自己」+ 两个人以上时那句「别把他们混成一个」。

    最后那句是这次改造的正题：分镜里「保持两人服饰与面容一致」这种写法本意是「别在镜头里
    忽然换装」，字面读却是「让两个人长得一样」。模型端只看字面，所以必须在这里说清楚。
    """
    lines: list[str] = []
    for p in book.subjects:
        head = f"{p.subject_tag}（{p.name}）" if p.name else p.subject_tag
        lines.append(
            f"{head} (appears in {shot}): fully_preserved - "
            f"{_RETAIN.get(p.kind, _RETAIN_ELSE)} {p.tag} unchanged; "
            "do not blend it with any other subject."
        )
    people = [p for p in book.subjects if p.is_person]
    if len(people) > 1:
        lines.append(
            f"{_and_join([p.subject_tag for p in people])} are different people: never copy the "
            "face, hair or costume of one onto another, and never merge two of them into one "
            "person, even if the shot description asks for a consistent look."
        )
    return "\n".join(lines) or "none"


def _detail(req: VideoRequest, shot: str) -> str:
    """画面那一段：`[Shot n] 视觉描述 Camera Motion: 机位`。"""
    if not req.segments:
        #: 自由文本 prompt：原样送，不硬套三段（也不给它编一个 `[Shot n]` 前缀）。
        return str(req.prompt or "").strip() or shot
    visual = str(req.segments.get("visual_prompt") or "").strip()
    camera = str(req.segments.get("camera_motion") or "").strip()
    body = f"{shot} {visual}".strip()
    return f"{body} Camera Motion: {camera}" if camera else body


def _soundscape(req: VideoRequest) -> str:
    """声音那一段：只有对白、同期环境声与必要动作音效（`AUDIO_RULE` 的口径）。"""
    return str(req.segments.get("audio_dialogue") or "").strip() or "none"


@dataclass(slots=True)
class AudioRequest:
    """一次**音源**请求。与 `VideoRequest` 分开是这一轮的核心取舍。

    AI 出的那条音轨往往很差，而以前想换掉它只能把整段画面重跑一次——几分钟的显存与时间，
    只为采一段声音。所以声音独立成一条链：同一个镜头上多出一版 `kind="audio"` 的版本
    （`Shot.current_audio_version_id`），画面一个字节都不用重跑。

    `text` 与 `prompt` 是两件事，故意不合成一个字段：`text` 是**要说的话**（对白，进 TTS
    那类图的文本框），`prompt` 是**声音长什么样**（「低沉的男声，雨声背景」，进音频生成图的
    描述框）。合成一个的话，一份只收台词的图会把「低沉的男声」当台词念出来。

    `source_video` 是这个镜头的画面：对口型那类图要它，纯 TTS 用不上——**给了但图里没有
    对应入口时只降级并留一条 note**，不失败（模型端那份图由模型端维护）。
    """

    text: str = ""
    prompt: str = ""
    negative: str = ""
    #: 音色参考（一段谁的声音）。它是**音频**文件，与 `VideoRequest.refs` 里的参考音频
    #: 不是同一个位置：那些是喂给画面模型的，这一条是喂给音源模型的。
    voice_ref: Path | None = None
    source_video: Path | None = None
    duration: float = 4.0
    seed: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    #: 降级说明，与 `VideoRequest.notes` 同一个作风：降级要说出来并冻结进版本。
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ImageRequest:
    """一次**出图**请求（角色四视图 / 地点参考图 / 道具图 / 镜头首尾帧候选）。

    这是第三条生成链。它与 `VideoRequest` 分开的理由和音频一样：另一份图、另一个地址、
    另一份密钥（`settings.image_*`），共用一个形状就得在业务层写 `if 这次是图片`。

    `refs` **复用** `RefAsset`（不另造一套）：图生图与风格参考走它，顺序即优先级。
    接不了参考图的端**只降级并留一条 note**，不失败——照 `AudioRequest` 那条规矩。

    `size` 是 `"宽x高"` 的字符串（`"1024x1024"`）：各家 API 的字段名与取值全不一样，
    在这里拆成两个 int 只会在适配器里再拼回去。拆分由适配器自己做（`size_wh()`）。
    """

    prompt: str = ""
    negative: str = ""
    size: str = "1024x1024"
    #: 图生图 / 风格参考。空列表 = 纯文生图。
    refs: list[RefAsset] = field(default_factory=list)
    seed: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    #: 降级说明，与 `VideoRequest.notes` / `AudioRequest.notes` 同一个作风：
    #: 降级要说出来并留档，而不是抛错让整个任务失败。
    notes: list[str] = field(default_factory=list)

    def size_wh(self, fallback: tuple[int, int] = (1024, 1024)) -> tuple[int, int]:
        """把 `size` 拆成 (宽, 高)。**认不出就回默认值，绝不抛**——出图这件事不该被
        一句写歪的 `"1024*1024"` 卡死在提交之前。"""
        raw = str(self.size or "").strip().lower().replace("*", "x").replace("×", "x")
        parts = raw.split("x", 1)
        if len(parts) != 2:
            return fallback
        try:
            width, height = int(parts[0].strip()), int(parts[1].strip())
        except ValueError:
            return fallback
        return (width, height) if width > 0 and height > 0 else fallback


@dataclass(slots=True)
class TaskState:
    """轮询结果。`detail` 是给人看的一句话，失败时它会进错误的 detail。"""

    status: str
    progress: float = 0.0
    detail: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


class VideoProvider(Protocol):
    """一个视频生成服务要能做的四件事 + 一个问句。"""

    name: str

    def ref_capacity(self) -> RefCapacity:
        """一次能收几个参考素材（按媒体各一个数）。**同步**，因为它只读本地那份图，不出网——
        上下文账单、编排账单、界面上每一处都要问它，出网的话这些只读路径全得变慢。
        查不出来（没选预设、文件坏了）一律回「不限制」，绝不在只读路径上抛错。
        """
        ...

    async def probe(self) -> dict[str, Any]:
        """配置页的「测试连接」。连不上要抛带建议的 AppError，不要返回 False。"""
        ...

    async def submit(self, req: VideoRequest, *, client_id: str) -> str: ...

    async def poll(self, task_id: str) -> TaskState: ...

    async def fetch(self, task_id: str) -> tuple[str, bytes]:
        """取回产物：(文件名, 字节)。素材必须落进工程，不能只存在服务端。"""
        ...


class AudioProvider(Protocol):
    """一个**音源**服务要能做的四件事。

    形状与 `VideoProvider` 一模一样（`probe` / `submit` / `poll` / `fetch`），只有请求类型
    不同——于是 `GenerationService` 里那套「提交 → 轮询 → 取回 → 登记成版本」一行都不用改。
    刻意不共用一个 provider 名字：音频那份图、地址、密钥与视频全是另一套
    （`settings.audio_*`），共用一个名字就得在业务层写 `if 这次是音频`。

    没有 `ref_capacity()`：音源图只收一个音色参考，不存在「槽位不够丢了哪几张」这件事。
    """

    name: str

    async def probe(self) -> dict[str, Any]: ...

    async def submit(self, req: AudioRequest, *, client_id: str) -> str: ...

    async def poll(self, task_id: str) -> TaskState: ...

    async def fetch(self, task_id: str) -> tuple[str, bytes]: ...


class ImageProvider(Protocol):
    """一个**出图**服务要能做的四件事。

    四个方法与 `VideoProvider` / `AudioProvider` **同名同形是刻意的**：
    `GenerationService._await_task()` 那个轮询循环（取消检查、每 5 拍发 `job.progress`、
    失败翻成 `WORKFLOW_ERROR`）于是一行不改就能给图片链用。

    云端出图 API 绝大多数是**同步**的（一次 POST 就回图），所以适配器里有一层
    「同步端 → 任务形状」的壳（`providers/image.py::ImageProtocol`）：`submit` 真的把图生出来
    并把字节存在内存里，`poll` 立刻回 done，`fetch` 把它弹出来。这层壳只存在于适配器内部，
    业务层看到的仍然只有这四个方法。

    没有 `ref_capacity()`：出图这条路上「参考图喂不进去」是端的能力问题
    （`supports_refs`），不是可数的槽位，降级说明直接写进 `req.notes`。
    """

    name: str

    async def probe(self) -> dict[str, Any]: ...

    async def submit(self, req: ImageRequest, *, client_id: str) -> str: ...

    async def poll(self, task_id: str) -> TaskState: ...

    async def fetch(self, task_id: str) -> tuple[str, bytes]: ...

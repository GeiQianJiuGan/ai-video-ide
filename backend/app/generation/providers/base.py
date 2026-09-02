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

from collections.abc import Sequence
from dataclasses import dataclass, field
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

#: 一条素材说明最多带多少字进 prompt。**截断规则只有这一处**（`ref_hint` 用它）：
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
    与底部控制台里；`desc` 只服务于提示词，由 `ref_hint()` 截断后单独渲染。
    空 = 用户没写，此时那句说明与升级前逐字相同。
    """

    path: Path
    label: str = ""
    kind: str = ""
    media: str = "image"
    desc: str = ""

    @property
    def media_label(self) -> str:
        return MEDIA_LABEL.get(self.media, "参考素材")


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
    """把「第几个参考素材是谁」写成一句话。

    给**只按顺序收素材、不接收标签**的模型端用（ComfyUI 那类图就是这样）：不说清楚的话，
    模型只知道多了几个输入，不知道哪个是主角。空列表回空串，调用方照此决定要不要拼。

    序号**按媒体各自从 1 数**，因为槽位就是按媒体分开的：图片进 `AIVS_REF_1`、
    视频进 `AIVS_REF_VIDEO_1`，混在一起连续编号的话这句说明会和真正填进去的槽位错位。

    有描述的素材多一个括号：`参考图1=阿岚（默认形象）（褪色军绿夹克，短发）`。
    **没有描述时输出与升级前逐字相同**——老工程的 prompt 不该因为多了一列而变样。
    """
    parts: list[str] = []
    for media, group in refs_by_media(refs).items():
        label = MEDIA_LABEL.get(media, "参考素材")
        for i, r in enumerate(group, 1):
            desc = clip_desc(r.desc)
            who = r.label or r.path.name
            parts.append(f"{label}{i}={who}（{desc}）" if desc else f"{label}{i}={who}")
    if not parts:
        return ""
    return f"参考素材说明：{'；'.join(parts)}。"


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

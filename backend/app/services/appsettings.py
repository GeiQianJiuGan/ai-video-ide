"""应用级设置（可写配置）。

配置一直只能靠 `AIVS_` 环境变量或 `.env`——那对「在界面里换一个 LLM 地址」不够用。
这里加一层**可写覆盖**：`runtime_dir/settings.json`，与 `library.json` / `recent.json`
同级，属于「这台机器怎么用这个应用」而不是某个工程的数据。

生效顺序 **settings.json → 环境变量（含 .env） → 代码默认**。

实现刻意不引入 `effective()` 间接层：文件里的覆盖在启动时**写进 `settings` 单例**，
于是全部既有读取点（`settings.llm_provider`、`settings.comfy_base_url`…）一行不用改，
测试里的 `monkeypatch.setattr(settings, ...)` 也照旧有效。

每个字段都回一个 `source`（`file` / `env` / `default`），照 `core/ffmpeg.py::Located.source`
的做法——「你看到的这个值是从哪来的」在排查时是唯一有用的信息。
密钥永不回明文：只回 `masked` 与 `has_value`。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.ai import prompts
from app.ai.llm import client as llm
from app.ai.llm import protocols as llm_protocols
from app.core.config import Settings, settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.generation.providers import image as image_protocols
from app.persistence.models import utc_now

log = get_logger("appsettings")


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """一个可配置字段：对外的点号键 → `Settings` 上的属性。"""

    key: str
    attr: str
    group: str
    label: str
    kind: str = "str"  # str | int | float | bool | secret | enum | text
    choices: tuple[str, ...] = ()
    #: 与 choices 一一对应的人话标签。空表示直接显示 choices 里的值。
    choice_labels: tuple[str, ...] = ()
    #: 这个字段配错了会导致什么做不出来。UI 直接显示，不在前端重写一遍。
    impact: str = ""
    #: 非空表示这个字段的取值**可以自动获取**（值就是 `POST /settings/models` 的 what）。
    #: 设置页照它画那个「自动获取」按钮，不在前端硬编码「模型这一项特殊」。
    fetch: str = ""
    #: `kind="text"` 用：留空时实际生效的那段内置文本。设置页把它当占位与「恢复内置默认」
    #: 的来源——内置提示词只有 `app/ai/prompts.py` 一份，前端绝不抄第二份。
    builtin: str = ""


#: 分组只影响配置页怎么摆，不影响存储结构。
GROUPS: tuple[tuple[str, str], ...] = (
    ("llm", "LLM（AI 协作）"),
    ("director", "AI 导演（免确认与一键全流程）"),
    ("prompt", "AI 提示词"),
    ("video", "视频生成 API"),
    ("audio", "音源生成（配音 / 环境音）"),
    ("image", "图片生成 API（角色 / 场景 / 道具参考图）"),
    ("refine", "二次处理（超分 / 插帧）"),
    ("ingest", "长视频导入切段"),
    ("comfy", "ComfyUI"),
    ("scene", "幕（流程图节点）"),
    ("runtime", "运行"),
)

FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec(
        "llm.provider",
        "llm_provider",
        "llm",
        "协议",
        "enum",
        # 协议表是唯一真源（app/ai/llm/protocols.py）：加一个协议不用改这里。
        tuple(llm_protocols.names()),
        tuple(llm_protocols.labels()),
        "none 时 AI 协作栏不可用；手动编排不受影响。",
    ),
    FieldSpec("llm.base_url", "llm_base_url", "llm", "地址", impact="留空则用协议的默认地址。"),
    FieldSpec(
        "llm.model",
        "llm_model",
        "llm",
        "模型",
        impact="没有模型名时视为未配置。",
        fetch="llm",
    ),
    FieldSpec("llm.api_key", "llm_api_key", "llm", "API Key", "secret"),
    FieldSpec(
        "llm.vision_model",
        "llm_vision_model",
        "llm",
        "看图模型",
        impact=(
            "「照着这张素材写一句描述」用哪个模型。留空就用上面那个主模型；"
            "本机端（Ollama / LM Studio）的主模型往往不认图，在这里单独指一个视觉模型。"
            "地址与密钥沿用上面那一套。"
        ),
        fetch="llm",
    ),
    # --- AI 导演的自动化程度。**这三项只改「谁按下那一下」，不改任何边界**：
    # 写工具照旧永不落库，落库照旧只走 `services/director.py::apply()` 那一份实现。
    FieldSpec(
        "director.auto_apply",
        "director_auto_apply",
        "director",
        "免确认模式（提案直接落库）",
        "bool",
        impact=(
            "开着时协作栏一轮产出的提案**在同一个请求里直接落库**，不再逐条审阅——"
            "落成了什么照旧一条条显示出来，接不上的名字与没排上的图也照旧说明。"
            "「一键全流程」要它开着才能跑（那一步要连着拆四轮，下一轮得用上一轮真落进库的 id）。"
            "关着是默认：数据库是你的，改它由你点头。"
        ),
    ),
    FieldSpec(
        "director.auto_image",
        "director_auto_image",
        "director",
        "全自动时顺带出参考图",
        "bool",
        impact=(
            "「一键全流程」新建角色 / 地点 / 道具时顺带排一张参考图（要先配好下面的"
            "「图片生成 API」）。关掉就只建素材——素材照旧建成，图这一项会写进回执。"
        ),
    ),
    FieldSpec(
        "director.max_scenes",
        "director_max_scenes",
        "director",
        "一键全流程最多拆几幕",
        "int",
        impact=(
            "它同时就是这一次要烧多少 token 的上限：分镜那一步**按幕各来一轮**。"
            "超出的部分不会被悄悄丢掉，会在回执里写明「这一次只拆了前 N 幕」。"
        ),
    ),
    FieldSpec(
        "prompt.breakdown",
        "prompt_breakdown",
        "prompt",
        "剧本拆解（分镜师）",
        "text",
        impact=(
            "决定 AI 把剧本拆成什么样的幕与镜头。留空用内置默认；"
            "JSON 输出形状与‘只处理对白/环境声、不生成配乐’的规则由系统始终追加，改不坏。"
        ),
        builtin=prompts.BREAKDOWN_TASK,
    ),
    FieldSpec(
        "prompt.director",
        "prompt_director",
        "prompt",
        "AI 导演（协作栏）",
        "text",
        impact=(
            "流程图右侧那个协作栏的角色与规则。留空用内置默认；"
            "「写工具只出提案、不落库」这条边界在代码里，提示词改不动它。"
        ),
        builtin=prompts.DIRECTOR_TASK,
    ),
    FieldSpec(
        "prompt.describe",
        "prompt_describe",
        "prompt",
        "素材描述（照着图写一句）",
        "text",
        impact=(
            "「AI 补全」照这段话给素材写描述——那句描述就是模型引用这个素材时唯一看得到的说明。"
            "留空用内置默认；「一段中文、不超过 120 字、只写看得见的事实、不要 JSON」"
            "由系统始终追加，改不坏。"
        ),
        builtin=prompts.DESCRIBE_TASK,
    ),
    FieldSpec(
        "video.provider",
        "video_provider",
        "video",
        "调用方式",
        "enum",
        ("comfy_preset", "http_api", "comfy_workflow"),
        ("ComfyUI 预设（默认）", "通用 REST API", "ComfyUI 工作流绑定（兼容）"),
        "comfy_preset 走模型端保存的图；http_api 走通用合同；comfy_workflow 是旧的绑定路径。",
    ),
    FieldSpec(
        "video.base_url",
        "video_base_url",
        "video",
        "服务地址",
        impact="http_api 时必填；comfy_preset 留空则用下面的 ComfyUI 地址。",
    ),
    FieldSpec("video.api_key", "video_api_key", "video", "API Key", "secret"),
    FieldSpec(
        "video.preset",
        "video_preset",
        "video",
        "默认预设（两个角色共用）",
        impact="comfy_preset 时指哪一份图；下面两项留空的角色按这一份出。缺它无法生成。",
    ),
    # 按角色再分一层：**工程没有单独绑预设时按这两项走**。R2V 与首尾帧要的入口本来就不一样，
    # 一台机器上常常是两份不同的图；只有上面那一个格子时，「工程没绑就跟随设置页」在首尾帧上
    # 必然落到一份不能用的图上。留空 = 退回上面那份共用的（照 refine.preset 的老作风）。
    FieldSpec(
        "video.r2v_preset",
        "video_r2v_preset",
        "video",
        "默认预设 · 普通镜头（R2V）",
        impact=(
            "工程没有单独绑预设时，普通镜头（图生视频）按这一份出。"
            "留空则退回上面那份共用的默认预设。"
        ),
    ),
    FieldSpec(
        "video.flf_preset",
        "video_flf_preset",
        "video",
        "默认预设 · 衔接与转场（首尾帧）",
        impact=(
            "工程没有单独绑预设时，首尾帧 / 转场 / FL2VA 按这一份出。它要的入口比 R2V 多两个"
            "（AIVS_FIRST_FRAME、AIVS_LAST_FRAME），所以常常是另一份图。"
            "留空则退回上面那份共用的默认预设。"
        ),
    ),
    FieldSpec("video.timeout", "video_timeout", "video", "单次超时（秒）", "int"),
    # 这里刻意**没有**「参考图上限」这一项：能收几张是模型端那份图的事实
    # （预设里数 AIVS_REF_* 槽位），由适配层的 ref_capacity() 回答。配一个数字只会和
    # 真实槽位打架，还得用户自己去对；超出槽位时改成生成前警告 + 确认。
    FieldSpec(
        "video.ref_labels",
        "video_ref_labels",
        "video",
        "在 prompt 里写明参考图是谁",
        "bool",
        impact=(
            "把「参考图1=林小雨（常服）」拼到 prompt 末尾。ComfyUI 那类图收不到标签，"
            "只能靠这句话让模型知道哪张是主角；不想让它动 prompt 就关掉。"
        ),
    ),
    # --- 音源：与视频**完全独立的一套**。声音那条链跑的往往是另一台机器 / 另一个服务
    # （TTS 在 CPU 上就够，视频要 24G 显存），所以地址、密钥、超时、预设都不共用。
    # `none` 是默认：没配音源不是异常，手动导入音频那条路走完全流程（硬约束 2）。
    FieldSpec(
        "audio.provider",
        "audio_provider",
        "audio",
        "调用方式",
        "enum",
        ("none", "comfy_preset", "http_api"),
        ("不配（只手动导入音频）", "ComfyUI 预设", "通用 REST API"),
        "none 时配音按钮不可用，但「导入音频」照旧——装配、静音、配音轨都不受影响。",
    ),
    FieldSpec(
        "audio.base_url",
        "audio_base_url",
        "audio",
        "服务地址",
        impact="http_api 时必填；comfy_preset 留空则用下面的 ComfyUI 地址。",
    ),
    FieldSpec("audio.api_key", "audio_api_key", "audio", "API Key", "secret"),
    FieldSpec(
        "audio.preset",
        "audio_preset",
        "audio",
        "音源预设",
        impact=(
            "音源图是另存的一份图：把台词那个文本框标成 AIVS_AUDIO_TEXT"
            "（只出环境音的图标 AIVS_AUDIO_PROMPT 即可）。缺它无法生成声音。"
        ),
    ),
    FieldSpec("audio.timeout", "audio_timeout", "audio", "单次超时（秒）", "int"),
    # --- 图片：**第三条链**。角色四视图 / 地点参考图 / 道具图会被当参考素材喂进
    # AIVS_REF_*，没有它们，只喂一张首帧的镜头在几秒里就把人物形象丢掉了。
    # 与视频 / 音频同一个作风：另一份图、另一个地址、另一份密钥。`none` 是默认——
    # 手动上传一张图那条路走完全流程（硬约束 2）。
    FieldSpec(
        "image.provider",
        "image_provider",
        "image",
        "调用方式",
        "enum",
        # 协议表是唯一真源（app/generation/providers/image.py::BY_NAME）：
        # 加一家出图 API 只改那一张表，这里与前端都一行不用动。
        tuple(image_protocols.names()),
        tuple(image_protocols.labels()),
        "none 时「生成参考图」按钮不可用，但手动上传一张图照旧——参考素材照样喂得进去。",
    ),
    FieldSpec(
        "image.base_url",
        "image_base_url",
        "image",
        "服务地址",
        impact="留空则用协议自己的默认地址（comfy_preset 退回下面的 ComfyUI 地址）。",
    ),
    FieldSpec(
        "image.model",
        "image_model",
        "image",
        "模型",
        impact="云端端点用哪个模型（comfy_preset 不看它，它认的是下面那份预设）。",
        fetch="image",
    ),
    FieldSpec("image.api_key", "image_api_key", "image", "API Key", "secret"),
    FieldSpec(
        "image.preset",
        "image_preset",
        "image",
        "图片预设",
        impact=(
            "comfy_preset 时指哪一份 T2I 图（提示词 / 负向 / 种子 / 参考图槽位用的是同一批"
            "AIVS_* 标题，另外可选 AIVS_WIDTH / AIVS_HEIGHT）。缺它无法出图——"
            "出图用哪份图靠这里指名，不靠标题猜。"
        ),
    ),
    FieldSpec(
        "image.size",
        "image_size",
        "image",
        "画幅",
        impact="形如 1024x1024。ComfyUI 那条路只有在图里标了 AIVS_WIDTH / AIVS_HEIGHT 时才生效。",
    ),
    FieldSpec("image.timeout", "image_timeout", "image", "单次超时（秒）", "int"),
    # --- 二次处理：**刻意没有单独的地址 / 密钥**。超分与出画面跑在同一台 ComfyUI 上是常态，
    # 多一套地址只会多一处配错的地方；真要分开时换预设就够了。
    FieldSpec(
        "refine.preset",
        "refine_preset",
        "refine",
        "二次处理预设",
        impact=(
            "超分 / 插帧用哪一份图：它必须标了 AIVS_SOURCE_VIDEO（要处理的那一段从这里进去）。"
            "留空则退回视频那份默认预设。"
        ),
    ),
    # --- 长视频切段：全是切点参数，不涉及任何服务（只用内置 FFmpeg）。
    FieldSpec(
        "ingest.scene_threshold",
        "ingest_scene_threshold",
        "ingest",
        "画面切换灵敏度",
        "float",
        impact="0~1，越小切得越碎。认不出切点时会自动降级到对白停顿 / 固定长度。",
    ),
    FieldSpec(
        "ingest.min_segment",
        "ingest_min_segment",
        "ingest",
        "一段最短多少秒",
        "float",
        impact="比它短的切点会被合并（账单里的 merged_away）——一堆半秒碎片没人处理得了。",
    ),
    FieldSpec(
        "ingest.chunk_seconds",
        "ingest_chunk_seconds",
        "ingest",
        "兜底固定长度（秒）",
        "float",
        impact="画面与对白都认不出切点时，按这个长度铺满整段。",
    ),
    FieldSpec(
        "ingest.copy_warn_mb",
        "ingest_copy_warn_mb",
        "ingest",
        "多大就提醒复制会慢（MB）",
        "int",
        impact="超过它时账单里提醒可以「引用原地」，代价是工程不能整个拷走。",
    ),
    FieldSpec("comfy.base_url", "comfy_base_url", "comfy", "地址"),
    FieldSpec(
        "scene.node_limit",
        "scene_node_limit",
        "scene",
        "一幕里人物 / 地点的上限",
        "int",
        impact="人物与地点各自不能超过它（prompt 不受限，它是必填的那一个）。",
    ),
    FieldSpec(
        "runtime.worker_limit",
        "worker_limit",
        "runtime",
        "并发生成数",
        "int",
        impact="调大会同时挤占显存，OOM 时先把它调回 1。",
    ),
    FieldSpec(
        "runtime.ffmpeg_path",
        "ffmpeg_path",
        "runtime",
        "FFmpeg 路径",
        impact="裸名字 ffmpeg 表示用内置那份；写成路径就是指名要用它。",
    ),
)

BY_KEY = {f.key: f for f in FIELDS}


def mask(value: str) -> str:
    """密钥只回尾巴，让人能认出「填的是哪一把」，但拿不到它。"""
    text = str(value or "")
    if not text:
        return ""
    return f"{'*' * max(4, len(text) - 4)}{text[-4:]}" if len(text) > 4 else "*" * len(text)


class AppSettingsService:
    """settings.json 的读写 + 探测。进程内单例，与其它 service 同构。"""

    def __init__(self) -> None:
        #: 应用文件覆盖**之前**的值（= 环境变量或代码默认）。清除某个覆盖时要回到它。
        self._baseline: dict[str, Any] = {}
        self._loaded = False

    @property
    def path(self) -> Path:
        return settings.runtime_dir / "settings.json"

    # --- 存储 ---

    def _read(self) -> dict[str, Any]:
        path = self.path
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # 坏文件不该让应用起不来：退回环境变量，并在日志里说清楚。
            log.warning("appsettings.unreadable", path=str(path), error=str(exc))
            return {}
        raw = data.get("overrides") if isinstance(data, dict) else None
        return {k: v for k, v in raw.items() if k in BY_KEY} if isinstance(raw, dict) else {}

    def _write(self, overrides: dict[str, Any]) -> None:
        target = self.path
        tmp = target.with_suffix(".json.tmp")
        payload = {"kind": "aivs-settings", "saved_at": utc_now(), "overrides": overrides}
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(target)
        except OSError as exc:
            raise AppError(
                ErrorCode.DISK_FULL if exc.errno == 28 else ErrorCode.VALIDATION_ERROR,
                "设置写入失败",
                f"{target}: {type(exc).__name__}: {exc}",
                ["确认磁盘可写且空间充足", "或改用 AIVS_ 环境变量配置"],
            ) from exc

    # --- 生效 ---

    def apply(self) -> dict[str, Any]:
        """把文件里的覆盖写进 settings 单例。启动时与每次 PATCH 后都要调。"""
        overrides = self._read()
        if not self._loaded:
            self._baseline = {f.attr: getattr(settings, f.attr) for f in FIELDS}
            self._loaded = True
        for spec in FIELDS:
            raw = overrides.get(spec.key)
            value = self._baseline[spec.attr] if raw is None else _coerce(spec, raw)
            setattr(settings, spec.attr, value)
        return overrides

    def _source_of(self, key: str, overrides: dict[str, Any]) -> str:
        if key in overrides:
            return "file"
        spec = BY_KEY[key]
        default = Settings.model_fields[spec.attr].default
        # 环境变量与 .env 都会体现在「启动值 ≠ 代码默认」上，两者对用户是同一件事。
        return "env" if self._baseline.get(spec.attr) != default else "default"

    # --- 对外形状 ---

    def snapshot(self) -> dict[str, Any]:
        overrides = self.apply()
        fields = []
        for spec in FIELDS:
            value = getattr(settings, spec.attr)
            secret = spec.kind == "secret"
            fields.append(
                {
                    "key": spec.key,
                    "group": spec.group,
                    "label": spec.label,
                    "kind": spec.kind,
                    "choices": list(spec.choices),
                    "choice_labels": list(spec.choice_labels),
                    "fetch": spec.fetch,
                    "impact": spec.impact,
                    #: 留空时实际生效的那段内置文本（只有 kind="text" 有）。
                    "builtin": spec.builtin,
                    "source": self._source_of(spec.key, overrides),
                    "value": None if secret else value,
                    "masked": mask(str(value)) if secret else None,
                    "has_value": bool(value) if secret else None,
                }
            )
        return {
            "path": self.path.as_posix(),
            "groups": [{"id": gid, "title": title} for gid, title in GROUPS],
            "fields": fields,
            "llm": llm.status(),
            #: 每个协议的默认地址 / 要不要密钥 / 支不支持工具——设置页照它给提示，
            #: 不在前端抄一份「Anthropic 的地址长这样」。
            "llm_protocols": llm_protocols.listing(),
            #: 出图那条链的协议表（默认地址 / 要不要密钥 / 收不收参考图 / 要不要预设）。
            #: **原样投影**：加一家出图 API 时设置页一行不用改。
            "image_protocols": image_protocols.listing(),
        }

    async def patch(self, patch: dict[str, Any]) -> dict[str, Any]:
        """写覆盖。值为 `null` 表示**清除覆盖**，回到环境变量或默认。

        密钥与长文本（提示词）额外把 `""` 也当成清除：那两种字段的输入框在界面上就是
        「清空 = 恢复默认」，`""` 落盘只会变成一把空密钥或一段空提示词。
        """
        unknown = [k for k in patch if k not in BY_KEY]
        if unknown:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "有不认识的设置项",
                "、".join(unknown),
                ["只提交 GET /settings 里出现过的 key"],
                {"unknown": unknown},
            )
        overrides = self._read()
        for key, raw in patch.items():
            spec = BY_KEY[key]
            if raw is None or (spec.kind in ("secret", "text") and str(raw).strip() == ""):
                overrides.pop(key, None)
                continue
            overrides[key] = _coerce(spec, raw)
        self._write(overrides)
        self.apply()
        log.info("appsettings.patched", keys=sorted(patch))
        return self.snapshot()

    # --- 探测与自动获取 ---

    async def probe(self, what: str) -> dict[str, Any]:
        """配置页的「测试连接」。失败一律是四要素错误，不是一个红叉。"""
        if what == "llm":
            return await llm.probe()
        if what == "video":
            from app.generation.providers import registry

            return await registry.provider().probe()
        if what == "audio":
            #: 音源那条链是独立的一套（provider / 地址 / 密钥都分开），所以探测也分开——
            #: 视频通了不代表声音通了。没配时 `audio_provider()` 抛的
            #: `MISSING_CAPABILITY` 里已经写了手动导入那条出路，不在这里另写一份。
            from app.generation.providers import registry

            return await registry.audio_provider().probe()
        if what == "image":
            #: 出图是第三条链，同样独立探测：ComfyUI 通了不代表云端出图那个密钥是对的。
            #: 没配时 `image_provider()` 抛的 `MISSING_CAPABILITY` 里已经写了手动上传
            #: 那条出路，这里不再另写一份。
            from app.generation.providers import registry

            return await registry.image_provider().probe()
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "不认识的探测对象",
            f"what={what}",
            ["用 llm / video / audio / image"],
        )

    async def models(
        self,
        what: str,
        provider: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> dict[str, Any]:
        """自动获取某个字段的候选取值（LLM 模型 / 出图模型）。

        协议 / 地址 / 密钥可以是**还没保存**的那份：让用户先看到模型列表再决定存什么，
        而不是先存一份可能是错的配置。这些覆盖不落盘。
        """
        if what == "image":
            #: 出图那条链另一套地址与密钥，所以列表也另列一次——LLM 那把密钥列不出
            #: 出图端有哪些模型。返回形状与 LLM 那一支一致，设置页共用同一段渲染。
            return await image_protocols.list_models(
                protocol=provider, base_url=base_url, api_key=api_key
            )
        if what != "llm":
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "这一项没有可自动获取的取值",
                f"what={what}",
                ["当前只有 llm（模型列表）与 image（出图模型）支持自动获取"],
                {"what": what},
            )
        return await llm.list_models(provider=provider, base_url=base_url, api_key=api_key)


def _coerce(spec: FieldSpec, raw: Any) -> Any:
    """按字段类型收紧输入。写错了当场说清楚，绝不悄悄存一个坏值。"""
    try:
        if spec.kind == "int":
            value: Any = int(raw)
            if value <= 0:
                raise ValueError("必须为正整数")
        elif spec.kind == "float":
            value = float(raw)
        elif spec.kind == "bool":
            value = raw if isinstance(raw, bool) else str(raw).lower() in ("1", "true", "yes")
        else:
            value = str(raw).strip()
            if spec.kind == "enum" and value not in spec.choices:
                raise ValueError(f"只能是 {'、'.join(spec.choices)}")
    except (TypeError, ValueError) as exc:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            f"{spec.label} 的取值不合法",
            f"{spec.key} = {raw!r}：{exc}",
            ["按提示改正后重试", "或提交 null 清除这项覆盖，回到环境变量的值"],
            {"key": spec.key},
        ) from exc
    return value


app_settings = AppSettingsService()

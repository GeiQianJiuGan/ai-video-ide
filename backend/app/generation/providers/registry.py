"""按名字挑一个适配器。

service 层只调 `provider(名字)`，永远不出现 `if provider == "xxx"`——这是硬约束 1 的落点。
**这个名字从哪来**由 `services/route.py` 回答（工程级可继承：工程选了就按工程，没选跟随
设置页），不是这一层从 `settings` 里直接读——一个工程走 REST、另一个走预设是常态。

**三条路在这里一视同仁**：`comfy_preset`（按 `AIVS_*` 标题填）/ `http_api`（我们定的 REST
合同）/ `comfy_workflow`（按用户那份图的绑定表填）。最后那条以前长在
`GenerationService._run_legacy` 里、靠 `job.workflow_id` 触发，而那一列从来没被写过值——
于是选了它等于什么都没选。提成适配器之后就没有「兼容分支」这回事了。
"""

from __future__ import annotations

from typing import Any

from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.generation.providers import audio, image, presets
from app.generation.providers.base import AudioProvider, RefCapacity, VideoProvider
from app.generation.providers.comfy_preset import ComfyPresetProvider
from app.generation.providers.comfy_workflow import ComfyWorkflowProvider
from app.generation.providers.http_api import HttpApiProvider

#: 名字 → 构造函数。新增适配器只需要在这里加一行 + 在 config 的注释里写清它是什么。
BUILTIN: dict[str, Any] = {
    "comfy_preset": ComfyPresetProvider,
    "http_api": HttpApiProvider,
    "comfy_workflow": ComfyWorkflowProvider,
}

LABELS = {
    "comfy_preset": "ComfyUI 预设（默认）",
    "http_api": "通用 REST API",
    "comfy_workflow": "ComfyUI 工作流绑定",
}

_cache: dict[str, VideoProvider] = {}


def names() -> list[str]:
    return list(BUILTIN)


def listing() -> list[dict[str, Any]]:
    """设置页的下拉选项。

    `legacy` **恒为 `False`**，键本身留着不删：三条路已经一视同仁，但前端那个类型
    （`shared/api/settings.ts::ProviderRow`）与老客户端还在读它，为了一个恒假的布尔
    改型不值得。
    """
    return [{"name": name, "label": LABELS.get(name, name), "legacy": False} for name in names()]


def provider(name: str | None = None) -> VideoProvider:
    chosen = name or settings.video_provider
    factory = BUILTIN.get(chosen)
    if factory is None:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "不认识的视频调用方式",
            f"要用的 video.provider 是 {chosen!r}。",
            ["在设置页或概览页重新选择调用方式"],
            {"available": names()},
        )
    #: 适配器本身是无状态的（地址与密钥都在调用时从 settings 读），进程内复用一个就够。
    if chosen not in _cache:
        _cache[chosen] = factory()
    return _cache[chosen]


def ref_capacity(name: str | None = None) -> RefCapacity:
    """这条路一次能收几个参考素材（三种媒体各一个数）。**绝不抛错**，因为问它的全是只读路径。

    「上限」不是应用级设置里的一个数字，而是这条路的事实：
    ComfyUI 预设数它自己的 `AIVS_REF_*` / `AIVS_REF_VIDEO_*` / `AIVS_REF_AUDIO_*` 槽位；
    REST 合同整组发过去、不限数量；工作流绑定那条路只喂图片，几张取决于绑的那份图。
    查不出来一律「不限制」——凭空造一个数字只会白丢用户的角色图 / 场景图。

    **按工程 + 能力**的那个数走 `services/route.py::capacity()`：它先解析出这个工程这个
    能力走哪条路、用哪一份图，再来问这里或直接数绑定行。
    """
    chosen = name or settings.video_provider
    try:
        return provider(chosen).ref_capacity()
    except AppError:
        #: 不认识的调用方式：设置页那边会用四要素错误说清楚，这里不跟着把只读页面打断。
        return RefCapacity(None, chosen, "读不到这条调用方式的参考素材槽位，先不限数量。")



def reset() -> None:
    """测试用：换掉 settings 之后丢掉缓存的实例。"""
    _cache.clear()
    _audio_cache.clear()
    _image_cache.clear()
    presets.reset_cache()


# --- 音源：另一条链，另一套地址 / 密钥 / 预设（见 providers/audio.py）---

_audio_cache: dict[str, AudioProvider] = {}


def audio_listing() -> list[dict[str, Any]]:
    """设置页的音源调用方式下拉。`none` 是**默认且第一项**——没配音源不是异常状态。"""
    return [
        {"name": name, "label": audio.LABELS.get(name, name)} for name in ("none", *audio.BUILTIN)
    ]


def audio_configured(name: str | None = None) -> bool:
    return (name or settings.audio_provider) not in ("", "none")


def audio_provider(name: str | None = None) -> AudioProvider:
    """当前的音源服务。**没配就是 `MISSING_CAPABILITY`，不是崩溃**（硬约束 2）。

    建议里必须写明手动那条路：外面做好的一段音频导入成这个镜头的音频版本，
    装配、静音、配音轨全都照旧工作——音源服务只是省掉「自己去配音」这一步。
    """
    chosen = name or settings.audio_provider
    if not audio_configured(chosen):
        raise AppError(
            ErrorCode.MISSING_CAPABILITY,
            "还没有配置音源服务",
            "音源是独立的一条链（另一份图 / 另一个地址），默认不开启。",
            [
                "在设置页的「音源生成」里选调用方式并选一份音源预设",
                "或把外面做好的音频导入成这个镜头的音频版本——装配与静音照旧工作，"
                "画面一个字节都不用重跑",
            ],
            {"audio_provider": chosen},
        )
    factory = audio.BUILTIN.get(chosen)
    if factory is None:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "不认识的音源调用方式",
            f"设置里的 audio.provider 是 {chosen!r}。",
            ["在设置页重新选择音源调用方式"],
            {"available": ["none", *audio.BUILTIN]},
        )
    if chosen not in _audio_cache:
        _audio_cache[chosen] = factory()
    return _audio_cache[chosen]


# --- 图片：第三条链，协议表在 providers/image.py（那张表是唯一真源）---

_image_cache: dict[str, Any] = {}


def image_listing() -> list[dict[str, Any]]:
    """设置页的图片协议表。**原样投影 `image.listing()`**，这里不加工、不筛选——
    加一家 API 只改那一张 `BY_NAME`，这一层与前端都一行不动。
    """
    return image.listing()


def image_configured(name: str | None = None) -> bool:
    return (name or settings.image_provider) not in ("", image.NONE)


def image_provider(name: str | None = None) -> Any:
    """当前的出图服务。**没配就是 `MISSING_CAPABILITY`，不是崩溃**（硬约束 2）。

    建议里必须写明手动那条路：在角色 / 地点 / 道具页直接上传一张图，参考素材照旧
    喂进 `AIVS_REF_*`——图片服务只是省掉「自己去别处生成再导入」这一步。
    """
    chosen = str(name or settings.image_provider or "").strip()
    if not image_configured(chosen):
        raise AppError(
            ErrorCode.MISSING_CAPABILITY,
            "还没有配置图片生成服务",
            "出图是独立的一条链（另一份图 / 另一个地址 / 另一份密钥），默认不开启。",
            [
                "在设置页的「图片生成 API」里选一种调用方式并填好地址",
                image.MANUAL_WAY_OUT,
            ],
            {"image_provider": chosen},
        )
    if chosen not in _image_cache:
        #: `require()` 负责「不认识这个名字」那句四要素错误，这里不再判一遍。
        #: `provider()` 是协议自己回的实例：HTTP 那几支是它本身，ComfyUI 那支另有一个类。
        _image_cache[chosen] = image.require(chosen).provider()
    return _image_cache[chosen]

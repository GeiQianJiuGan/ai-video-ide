"""按设置挑一个适配器。

service 层只调 `provider()`，永远不出现 `if provider == "xxx"`——这是硬约束 2 的落点。
`comfy_workflow` 是旧的节点绑定路径，它不实现 `VideoProvider`（那条路在
`GenerationService._execute` 里单独保留），所以这里显式挡下来并说清怎么走。
"""

from __future__ import annotations

from typing import Any

from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.generation.providers import presets
from app.generation.providers.base import RefCapacity, VideoProvider
from app.generation.providers.comfy_preset import ComfyPresetProvider
from app.generation.providers.http_api import HttpApiProvider

#: 名字 → 构造函数。新增适配器只需要在这里加一行 + 在 config 的注释里写清它是什么。
BUILTIN: dict[str, Any] = {
    "comfy_preset": ComfyPresetProvider,
    "http_api": HttpApiProvider,
}

#: 不走适配层的调用方式：留着兼容老工程，但生成时是另一条分支。
LEGACY = ("comfy_workflow",)

LABELS = {
    "comfy_preset": "ComfyUI 预设（默认）",
    "http_api": "通用 REST API",
    "comfy_workflow": "ComfyUI 工作流绑定（兼容）",
}

_cache: dict[str, VideoProvider] = {}


def names() -> list[str]:
    return [*BUILTIN, *LEGACY]


def listing() -> list[dict[str, Any]]:
    """设置页的下拉选项。"""
    return [
        {"name": name, "label": LABELS.get(name, name), "legacy": name in LEGACY}
        for name in names()
    ]


def is_legacy(name: str | None = None) -> bool:
    return (name or settings.video_provider) in LEGACY


def provider(name: str | None = None) -> VideoProvider:
    chosen = name or settings.video_provider
    if chosen in LEGACY:
        raise AppError(
            ErrorCode.MISSING_CAPABILITY,
            "当前调用方式不走生成适配层",
            f"{LABELS.get(chosen, chosen)} 是旧的节点绑定路径，需要给镜头指定一个已校验的工作流。",
            [
                "在设置页把调用方式改成「ComfyUI 预设」（推荐，模型端的图由模型端维护）",
                "或在流程页给镜头绑定工作流后再生成",
            ],
            {"provider": chosen},
        )
    factory = BUILTIN.get(chosen)
    if factory is None:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "不认识的视频调用方式",
            f"设置里的 video.provider 是 {chosen!r}。",
            ["在设置页重新选择调用方式"],
            {"available": names()},
        )
    #: 适配器本身是无状态的（地址与密钥都在调用时从 settings 读），进程内复用一个就够。
    if chosen not in _cache:
        _cache[chosen] = factory()
    return _cache[chosen]


def ref_capacity(name: str | None = None) -> RefCapacity:
    """当前这条路一次能收几个参考素材（三种媒体各一个数）。**绝不抛错**，因为问它的全是只读路径。

    「上限」不再是应用级设置里的一个数字，而是这条路的事实：
    ComfyUI 预设数它自己的 `AIVS_REF_*` / `AIVS_REF_VIDEO_*` / `AIVS_REF_AUDIO_*` 槽位；
    REST 合同整组发过去、不限数量；旧的绑定路径压根不注入素材，也没有上限可言。
    查不出来一律「不限制」——凭空造一个数字只会白丢用户的角色图 / 场景图。
    """
    chosen = name or settings.video_provider
    if chosen in LEGACY:
        return RefCapacity(
            None,
            LABELS.get(chosen, chosen),
            "旧的工作流绑定路径不注入任何素材（连首帧都不注入），谈不上参考素材上限。"
            "要真的喂角色图 / 场景图请改用「ComfyUI 预设」或「通用 REST API」。",
        )
    try:
        return provider(chosen).ref_capacity()
    except AppError:
        #: 不认识的调用方式：设置页那边会用四要素错误说清楚，这里不跟着把只读页面打断。
        return RefCapacity(None, chosen, "读不到这条调用方式的参考素材槽位，先不限数量。")


def reset() -> None:
    """测试用：换掉 settings 之后丢掉缓存的实例。"""
    _cache.clear()
    presets.reset_cache()

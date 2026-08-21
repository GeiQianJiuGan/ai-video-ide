"""按设置挑一个适配器。

service 层只调 `provider()`，永远不出现 `if provider == "xxx"`——这是硬约束 2 的落点。
`comfy_workflow` 是旧的节点绑定路径，它不实现 `VideoProvider`（那条路在
`GenerationService._execute` 里单独保留），所以这里显式挡下来并说清怎么走。
"""

from __future__ import annotations

from typing import Any

from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.generation.providers.base import VideoProvider
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


def reset() -> None:
    """测试用：换掉 settings 之后丢掉缓存的实例。"""
    _cache.clear()

"""内置技能：视频写法、出图模板与模型无关的制帧叙事规划。

`render(name)` 共用一个读取入口；规划技能不进入视频渲染规范的名字表。
"""

from __future__ import annotations

from app.ai.skills.frame_video import (
    FRAME_VIDEO_NAMES,
    frame_video_catalog,
    frame_video_render,
)
from app.ai.skills.image_prompt import (
    IMAGE_NAMES,
    IMAGE_NEGATIVE,
    IMAGE_RULE,
    image_catalog,
    image_get,
    image_listing,
    image_pick,
    render_image_prompt,
)
from app.ai.skills.image_prompt import image_render as _image_render
from app.ai.skills.video_prompt import ALIASES, FILE_REFERENCES, NAMES, catalog, pick
from app.ai.skills.video_prompt import render as _video_render
from app.core.errors import AppError, ErrorCode

#: 所有技能与官方文件引用——`read_skill` 的 enum 统一使用这份清单。
ALL_NAMES = (*NAMES, *FILE_REFERENCES, *IMAGE_NAMES, *FRAME_VIDEO_NAMES)

__all__ = [
    "ALL_NAMES",
    "ALIASES",
    "FILE_REFERENCES",
    "FRAME_VIDEO_NAMES",
    "IMAGE_NAMES",
    "IMAGE_NEGATIVE",
    "IMAGE_RULE",
    "NAMES",
    "catalog",
    "frame_video_catalog",
    "frame_video_render",
    "image_catalog",
    "image_get",
    "image_listing",
    "image_pick",
    "pick",
    "render",
    "render_image_prompt",
]


def render(name: str) -> str:
    """先识别规划与出图技能，再解析视频规范名称、别名及文件路径。"""
    key = str(name or "").strip().lower()
    if key in FRAME_VIDEO_NAMES:
        return frame_video_render()
    if key in IMAGE_NAMES:
        return _image_render(key)
    try:
        return _video_render(name)
    except AppError as exc:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "没有这份 SKILL",
            f"name = {name or '（空）'}。",
            [
                f"写镜头 prompt 的规范名：{'、'.join(NAMES)}",
                f"官方文件引用路径：{'、'.join(FILE_REFERENCES)}",
                f"出参考图的是：{'、'.join(IMAGE_NAMES)}",
                f"制帧叙事规划：{'、'.join(FRAME_VIDEO_NAMES)}",
            ],
            {"name": name, "available": list(ALL_NAMES)},
        ) from exc

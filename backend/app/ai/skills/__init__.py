"""内置 SKILL 包：AI 按需取的那几份结构说明。

**两族**：写镜头 prompt 的那几份（`video_prompt.py`，MiniMax H3 官方规范）与出参考图的
那三份（`image_prompt.py`，角色四视图 / 场景 / 道具）。

`render(name)` **同时查两张表**，所以 `read_skill` 只需要一个工具、模型只需要记一个名字。
"""

from __future__ import annotations

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

#: 两族名字与官方文件引用合起来——`read_skill` 那个 enum 用它，别在工具层拼第二份。
ALL_NAMES = (*NAMES, *FILE_REFERENCES, *IMAGE_NAMES)

__all__ = [
    "ALL_NAMES",
    "FILE_REFERENCES",
    "IMAGE_NAMES",
    "IMAGE_NEGATIVE",
    "IMAGE_RULE",
    "NAMES",
    "catalog",
    "image_catalog",
    "image_get",
    "image_listing",
    "image_pick",
    "pick",
    "render",
    "render_image_prompt",
]


def render(name: str) -> str:
    """一份 SKILL 的全文。**两族共用这一个入口**（视频那几份 + 出图那三份）。

    出图那三份先认，认不出来再交给视频那一族解析（支持规范名、别名及官方文件路径）。
    """
    key = str(name or "").strip().lower()
    if key in IMAGE_NAMES:
        return _image_render(key)
    try:
        return _video_render(name)
    except AppError:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "没有这份 SKILL",
            f"name = {name or '（空）'}。",
            [
                f"写镜头 prompt 的规范名：{'、'.join(NAMES)}",
                f"官方文件引用路径：{'、'.join(FILE_REFERENCES)}",
                f"出参考图的是：{'、'.join(IMAGE_NAMES)}",
            ],
            {"name": name, "available": list(ALL_NAMES)},
        )

"""内置 SKILL 包：AI 按需取的那几份结构说明。

**两族**：写镜头 prompt 的那几份（`video_prompt.py`，按首尾帧形态分四种）与出参考图的
那三份（`image_prompt.py`，角色四视图 / 场景 / 道具）。

`render(name)` **同时查两张表**，所以 `read_skill` 只需要一个工具、模型只需要记一个名字
——两族各给一个工具的话，模型会拿写视频那份去出图（它们要的东西正好相反：视频要光影与
运镜，参考图要平光无构图）。名字在两族之间不重复，重复了就是设计错误。

往外只暴露这几个名字，两个 dataclass（`Skill` / `ImageSkill`）都是实现细节。
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
from app.ai.skills.video_prompt import NAMES, catalog, pick
from app.ai.skills.video_prompt import render as _video_render
from app.core.errors import AppError, ErrorCode

#: 两族名字合起来——`read_skill` 那个 enum 用它，别在工具层拼第二份。
ALL_NAMES = (*NAMES, *IMAGE_NAMES)

__all__ = [
    "ALL_NAMES",
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
    """一份 SKILL 的全文。**两族共用这一个入口**（视频那四份 + 出图那三份）。

    出图那三份先认，认不出来再交给视频那一族。**不认识的名字在这里就报**，
    而不是让 `video_prompt.render()` 去报：那边的建议里只有视频那四个名字，
    模型写了 `char_sheet` 却被告知「可用的是 flf、i2v…」只会让它再猜一遍。
    """
    key = str(name or "").strip().lower()
    if key in IMAGE_NAMES:
        return _image_render(key)
    if key not in NAMES:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "没有这份 SKILL",
            f"name = {name or '（空）'}。",
            [
                f"写镜头 prompt 的是：{'、'.join(NAMES)}",
                f"出参考图的是：{'、'.join(IMAGE_NAMES)}",
            ],
            {"name": name, "available": list(ALL_NAMES)},
        )
    return _video_render(key)

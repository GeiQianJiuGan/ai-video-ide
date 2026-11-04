"""手动编辑与 AI 导演共用的画面字段校验。"""

from typing import Literal

from pydantic import BaseModel, Field


class FrameCardPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    prompt: str | None = None
    story: str | None = None
    role: Literal["first", "key", "last"] | None = None
    position: int | None = Field(default=None, ge=0)
    refs: list[str] | None = None


class FrameContinuityInput(BaseModel):
    source_version_id: str
    # 数字为源视频绝对秒数，便于与实际裁剪位置对应。
    at: Literal["end"] | float = "end"
    use: Literal["opening", "reference"] = "opening"


class FrameVideoInput(BaseModel):
    prompt: str = ""
    duration: float = Field(default=4, gt=0, le=60)
    preset: str = ""
    first_card_id: str = ""
    last_card_id: str = ""

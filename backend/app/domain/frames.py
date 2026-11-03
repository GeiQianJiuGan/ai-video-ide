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

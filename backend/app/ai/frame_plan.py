"""完整分镜与已有镜头共用的制帧计划契约。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FrameDraft(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=200)
    role: Literal["first", "key", "last"]
    story: str = Field(min_length=10)
    prompt: str = Field(min_length=10)


class FramePlanDraft(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    frames: list[FrameDraft] = Field(min_length=1, max_length=8)
    video_prompt: str = Field(min_length=10)

    @model_validator(mode="after")
    def ordered_roles(self):
        roles = [frame.role for frame in self.frames]
        if roles[0] != "first" or roles.count("first") != 1:
            raise ValueError("frames 第一张必须为唯一的 first 开场画面")
        if roles.count("last") > 1 or ("last" in roles and roles[-1] != "last"):
            raise ValueError("frames 的 last 收尾画面至多一个，且必须排在最后")
        return self

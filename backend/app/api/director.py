"""AI 导演接口（幕流程图右栏的后端）。

三个端点，界线就是「提案」与「落库」的界线：

  - `POST /director/chat`  说一句话 → 拿回一份提案，**数据库一行不动**；
  - `POST /director/apply` 把审阅通过的条目落库，只落 `op != "reject"` 的；
  - `GET  /director`       历史对话与提案（刷新页面不丢）+ LLM 状态
    （未配置时前端据此显示去配置页的引导，而不是一个红叉）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.services.director import director

router = APIRouter(tags=["director"])


class ChatBody(BaseModel):
    message: str = Field(description="想让 AI 做什么，例如「在第 2 幕后面加一幕雨夜追车」")
    #: 用户现在开着哪一页。**只影响这一次请求拼的系统提示词**那一句提示，不落库——
    #: 剧本页与幕流程图共用同一个会话，换页不该让历史对话变味。
    scope: str = Field(default="flow", description="script（剧本页）/ flow（幕流程图页）")


class ApplyBody(BaseModel):
    ops: list[dict[str, Any]] = Field(
        default_factory=list, description="审阅过的提案条目；丢弃的那条把 op 改成 reject"
    )


@router.get("/projects/{pid}/director")
async def history(pid: str) -> dict[str, Any]:
    return await director.history(pid)


@router.post("/projects/{pid}/director/chat", status_code=201)
async def chat(pid: str, body: ChatBody) -> dict[str, Any]:
    """产出提案。落库的是「提案」这条记录，不是提案里的改动。"""
    return await director.chat(pid, body.message, body.scope)


@router.post("/projects/{pid}/director/apply", status_code=201)
async def apply(pid: str, body: ApplyBody) -> dict[str, Any]:
    return await director.apply(pid, body.ops)


@router.delete("/projects/{pid}/director", status_code=204)
async def clear(pid: str) -> None:
    """清空协作记录。已经落库的改动不受影响。"""
    await director.clear(pid)

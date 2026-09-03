"""AI 导演接口（全局协作栏的后端）。

五个端点，界线就是「提案」与「落库」的界线：

  - `POST /director/chat`        说一句话 → 拿回一份提案，**数据库一行不动**；
  - `POST /director/chat/stream` 同一件事，边跑边吐（SSE）。上面那条原样保留：
    不支持 SSE 的调用方与后端自己的测试都还走它；
  - `POST /director/apply`       把审阅通过的条目落库，只落 `op != "reject"` 的；
  - `POST /director/attach`      一份 .docx / .xlsx / … → 一段纯文本，**只填输入框**：
    不落库、不落盘、不出网，也**不要求配好 LLM**（用户得先看见抽出来什么）；
  - `GET  /director`             历史对话与提案（刷新页面不丢）+ LLM 状态
    （未配置时前端据此显示去配置页的引导，而不是一个红叉）+ 附件能收什么。

这一层照旧极薄：SSE 那条也只是**把 service 给的事件按 `event:` / `data:` 写出去**，
一个业务判断都不做。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, File, UploadFile
from fastapi.responses import StreamingResponse
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


def _sse(event: str, data: Any) -> bytes:
    """一条 SSE 帧。`data` 永远是一行 JSON——多行 data 的拼接规则不值得让前端去处理。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


@router.post("/projects/{pid}/director/chat/stream")
async def chat_stream(pid: str, body: ChatBody) -> StreamingResponse:
    """产出提案，边跑边吐。**和不流式那条落的是同一份记录，业务数据照旧一行不动。**

    先 `await stream_precheck()`：消息空的 / LLM 没配 / 工程没打开都在开流之前抛，
    前端拿到的是正常的 JSON 四要素错误。开流之后的失败才走 `error` 事件——
    `done` 与 `error` 互斥且必有其一。

    两个头是给中间那层代理的：`no-cache` 不许缓存，`X-Accel-Buffering: no` 让 nginx
    别攒着一起发（攒着就等于不流式了）。
    """
    await director.stream_precheck(pid, body.message)

    async def frames() -> AsyncIterator[bytes]:
        async for event in director.chat_stream(pid, body.message, body.scope):
            yield _sse(event["event"], event["data"])

    return StreamingResponse(
        frames(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/projects/{pid}/director/apply", status_code=201)
async def apply(pid: str, body: ApplyBody) -> dict[str, Any]:
    return await director.apply(pid, body.ops)


@router.post("/projects/{pid}/director/attach")
async def attach(pid: str, file: UploadFile = File(...)) -> dict[str, Any]:
    """一份附件 → 一段纯文本，回给前端填进输入框。**什么都不落，也不出网。**

    刻意**不是** 201：这一下没有创建任何东西——没有记录、没有文件、没有资产。
    """
    data = await file.read()
    return await director.attach(pid, file.filename or "附件", data)


@router.delete("/projects/{pid}/director", status_code=204)
async def clear(pid: str) -> None:
    """清空协作记录。已经落库的改动不受影响。"""
    await director.clear(pid)

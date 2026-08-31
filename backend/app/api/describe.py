"""「照着素材写一句描述」接口。

两个端点，界线还是「账单」与「动手」那条（照 `api/images.py` 的作风）——只是这条链
**两头都不落库**：

  - `POST /describe/plan`     账单：用哪个端、能不能真的看图、这几张现在有没有描述、
    图会不会真的送出去、缺什么。只读、不出网；
  - `POST /describe/suggest`  让模型各写一句。**回的是建议文字，一行库都不改**。

落库只有一条路：用户在界面上按保存 → `PATCH /projects/{pid}/assets/{asset_id}`。
AI 协作栏那条路同理，只出 `set_description` 提案，`POST /director/apply` 才落。

极薄：Pydantic body + 转调 `services/describe.py`。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.services.describe import describe

router = APIRouter(tags=["describe"])


class DescribeBody(BaseModel):
    #: 要补描述的素材。**只认显式传进来的那几个**——服务端不替用户挑「该给哪些补」，
    #: 想先看看缺哪些请用 `GET /projects/{pid}/assets/undescribed`。
    asset_ids: list[str] = Field(default_factory=list, description="资产 id，一个或多个")


@router.post("/projects/{pid}/describe/plan")
async def plan(pid: str, body: DescribeBody) -> dict[str, Any]:
    """先账单再动手：端没配好、端不认图、图太大送不出去，在这里就说清楚。"""
    return await describe.plan(pid, body.asset_ids)


@router.post("/projects/{pid}/describe/suggest")
async def suggest(pid: str, body: DescribeBody) -> dict[str, Any]:
    """出建议。**不落库**——每条回一句 `suggestion`，界面填进输入框，用户按保存才写。"""
    return await describe.suggest(pid, body.asset_ids)

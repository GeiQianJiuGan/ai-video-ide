"""长视频导入接口：**导入一段成片 → 出账单 → 切成一幕若干镜头。**

四个端点，刻意分成三步（与 `adopt` / `sequence` 同一个习惯）：
  · `ingest/register` —— 把源文件登记进来（默认复制进工程，可选原地引用）；
  · `ingest/plan`     —— 只读地出账单：切几段、每段哪儿到哪儿、哪些切点被合并了；
  · `ingest/run`      —— 才落库：一幕 `kind="ingested"` + 每段一个镜头一版（零文件复制）。

这条路上**没有剧本、没有 LLM**：`POST /ingest/run` 不碰任何 AI 入口，`llm_provider="none"`
也照样走完。产出的幕就是一幕普通的幕，顺序、时间线装配、导出、二次处理全部复用；
默认排在最后，要放到剧本前面用已有的 `POST /scenes/reorder`（或 `run` 时给 `position`）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.services.ingest import METHOD_LABEL, ingest

router = APIRouter(tags=["ingest"])


class RegisterBody(BaseModel):
    path: str = Field(description="本机上那段成片的绝对路径")
    #: 不叫 `copy`：那个名字会遮住 `BaseModel.copy`，pydantic 会当场警告。
    copy_into_project: bool = Field(
        default=True,
        description="复制进工程（默认）。false = 原地引用，省磁盘但源文件一移动就全失效",
    )


class PlanBody(BaseModel):
    asset_id: str = Field(description="已登记的源视频资产 id（先调 ingest/register）")
    method: str = Field(default="auto", description="auto / scene / silence / fixed")
    threshold: float | None = Field(default=None, description="画面切换灵敏度，越小切得越碎")
    min_segment: float | None = Field(default=None, description="一段最短多少秒，更短的切点合并")
    max_segment: float | None = Field(default=None, description="一段最长多少秒，超过时自动细分")
    chunk_seconds: float | None = Field(default=None, description="兜底固定窗口的长度")
    cuts: list[float] | None = Field(
        default=None, description="手动切点（秒）。给了就不跑自动检测——账单上拖过的切点原样落下去"
    )


class RunBody(PlanBody):
    title: str | None = Field(default=None, description="这一幕叫什么；留空按源文件名")
    prompt: str | None = Field(
        default=None, description="整幕共用的 prompt（镜头留空就继承它，改一处全段跟着变）"
    )
    param_mode: str = Field(
        default="shared", description="shared（共用参数，默认）/ per_shot（每段独立）"
    )
    position: int | None = Field(
        default=None, description="插到第几幕（1 起）。留空排在最后；插到中间会回「N 幕顺序后移」"
    )


@router.get("/ingest/methods")
async def methods() -> list[dict[str, Any]]:
    """有哪几种切点方法。方法是后端的事实，前端不要硬编码这几个字符串。"""
    return [{"method": "auto", "label": "自动（画面切换 → 对白停顿 → 固定长度）"}] + [
        {"method": k, "label": v} for k, v in METHOD_LABEL.items() if k != "manual"
    ]


@router.post("/projects/{pid}/ingest/register", status_code=201)
async def register(pid: str, body: RegisterBody) -> dict[str, Any]:
    """登记一段成片。`copy_into_project=false` 时返回值里的 `warnings` 必须显示出来
    （可移植性真的丢了）。"""
    return await ingest.register(pid, body.path, copy=body.copy_into_project)


@router.post("/projects/{pid}/ingest/plan")
async def plan(pid: str, body: PlanBody) -> dict[str, Any]:
    """只出账单：切成几段、每段多长、哪几个切点太短被合并了、用哪一级方法认出来的。

    **一行都不落库、一个文件都不切。** 切点是建议不是判决——改完再走 `run`。
    """
    return await ingest.plan(
        pid,
        body.asset_id,
        method=body.method,
        threshold=body.threshold,
        min_segment=body.min_segment,
        max_segment=body.max_segment,
        chunk_seconds=body.chunk_seconds,
        cuts=body.cuts,
    )


@router.post("/projects/{pid}/ingest/run", status_code=201)
async def run(pid: str, body: RunBody) -> dict[str, Any]:
    """按账单落库。**零文件复制**：每段一版，`asset_id` 都指向同一个源文件 + 各自的区间。"""
    return await ingest.run(
        pid,
        body.asset_id,
        title=body.title,
        prompt=body.prompt,
        method=body.method,
        threshold=body.threshold,
        min_segment=body.min_segment,
        max_segment=body.max_segment,
        chunk_seconds=body.chunk_seconds,
        cuts=body.cuts,
        param_mode=body.param_mode,
        position=body.position,
    )

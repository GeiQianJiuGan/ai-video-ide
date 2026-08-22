"""视频生成适配层：与模型无关的形状。

生成层不再由本工具维护 ComfyUI 的图。这里只定义「一次 R2V 请求长什么样」与
「一个服务要能做哪四件事」，具体差异全部关在同目录的适配器里——
service 层永远不出现 `if provider == "xxx"`。

本轮只有 R2V（图 → 视频）：
  · `i2v` 只给首帧；
  · `flf` 给首尾帧（两幕之间那段 1~2s 转场就是它）。
T2V 暂不做——没有首帧的镜头在编排时就会被账单挡下来，而不是生成出一段跑偏的画面。

**首尾帧和参考图不是一回事**，所以是两个字段：首尾帧决定「画面从哪一格开始 / 结束」，
参考图决定「谁出场、长什么样、在哪儿」。只喂一张首帧时最容易丢的就是人物形象——
账单里算出来的角色表 / 地点参考图必须能一起送到模型端，这就是 `refs` 存在的理由。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

MODES = ("i2v", "flf")

#: 任务状态的统一口径，与 Job.status 对齐，适配器负责把各家的说法翻译成这四个。
STATUSES = ("queued", "running", "done", "failed")


@dataclass(frozen=True, slots=True)
class RefImage:
    """一张参考图：图在哪 + 它是谁。

    `label` / `kind` 直接来自上下文账单（角色表 / 地点参考 / 道具参考）。模型端接不接收
    这句说明由适配器决定——但**不许因为带不了标签就把图丢掉**。
    """

    path: Path
    label: str = ""
    kind: str = ""


@dataclass(slots=True)
class VideoRequest:
    """一次生成请求。`extra` 原样透传给模型端，本工具不解释里面的东西。"""

    mode: str
    prompt: str = ""
    negative: str = ""
    first_frame: Path | None = None
    last_frame: Path | None = None
    #: 首尾帧之外的参考图，按账单顺序（优先级高的在前）。
    refs: list[RefImage] = field(default_factory=list)
    duration: float = 4.0
    seed: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    #: 适配器提交时写下的降级说明，例如「这份图只有 3 个参考图槽位，账单里第 4 张没喂进去」。
    #: service 层原样冻结进版本，不解释内容——「绝不静默失败」在这里的样子是
    #: 「降级也要说出来并留档」，而不是抛错让整个任务失败。
    notes: list[str] = field(default_factory=list)


def ref_hint(refs: Sequence[RefImage]) -> str:
    """把「第几张参考图是谁」写成一句话。

    给**只按顺序收图、不接收标签**的模型端用（ComfyUI 那类图就是这样）：不说清楚的话，
    模型只知道多了几张图，不知道哪张是主角。空列表回空串，调用方照此决定要不要拼。
    """
    if not refs:
        return ""
    body = "；".join(f"参考图{i}={r.label or r.path.name}" for i, r in enumerate(refs, 1))
    return f"参考图说明：{body}。"


@dataclass(slots=True)
class TaskState:
    """轮询结果。`detail` 是给人看的一句话，失败时它会进错误的 detail。"""

    status: str
    progress: float = 0.0
    detail: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


class VideoProvider(Protocol):
    """一个视频生成服务要能做的四件事。"""

    name: str

    async def probe(self) -> dict[str, Any]:
        """配置页的「测试连接」。连不上要抛带建议的 AppError，不要返回 False。"""
        ...

    async def submit(self, req: VideoRequest, *, client_id: str) -> str: ...

    async def poll(self, task_id: str) -> TaskState: ...

    async def fetch(self, task_id: str) -> tuple[str, bytes]:
        """取回产物：(文件名, 字节)。素材必须落进工程，不能只存在服务端。"""
        ...

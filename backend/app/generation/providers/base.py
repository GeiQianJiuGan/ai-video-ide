"""视频生成适配层：与模型无关的形状。

生成层不再由本工具维护 ComfyUI 的图。这里只定义「一次 R2V 请求长什么样」与
「一个服务要能做哪四件事」，具体差异全部关在同目录的适配器里——
service 层永远不出现 `if provider == "xxx"`。

本轮只有 R2V（图 → 视频）：
  · `i2v` 只给首帧；
  · `flf` 给首尾帧（两幕之间那段 1~2s 转场就是它）。
T2V 暂不做——没有首帧的镜头在编排时就会被账单挡下来，而不是生成出一段跑偏的画面。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

MODES = ("i2v", "flf")

#: 任务状态的统一口径，与 Job.status 对齐，适配器负责把各家的说法翻译成这四个。
STATUSES = ("queued", "running", "done", "failed")


@dataclass(slots=True)
class VideoRequest:
    """一次生成请求。`extra` 原样透传给模型端，本工具不解释里面的东西。"""

    mode: str
    prompt: str = ""
    negative: str = ""
    first_frame: Path | None = None
    last_frame: Path | None = None
    duration: float = 4.0
    seed: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def frames(self) -> list[Path]:
        return [p for p in (self.first_frame, self.last_frame) if p is not None]


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

"""内置 SKILL 包：给 AI 写镜头 prompt 时按需取的那几份结构说明。

现在只有一族（四种参考图形态下的视频 prompt，见 `video_prompt.py`）。往外只暴露
`catalog` / `render` / `pick` 与 `NAMES` —— 工具层与提示词层都只认这四个名字，
`Skill` 那个 dataclass 是实现细节。
"""

from __future__ import annotations

from app.ai.skills.video_prompt import NAMES, catalog, pick, render

__all__ = ["NAMES", "catalog", "pick", "render"]

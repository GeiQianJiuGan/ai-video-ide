"""模型无关的制帧叙事技能，与视频渲染规范分别发现。"""

from app.ai.skills.video_prompt import _skill_dir
from app.core.errors import AppError, ErrorCode

FRAME_VIDEO_NAME = "frame-video-planning"
FRAME_VIDEO_NAMES = (FRAME_VIDEO_NAME,)


def frame_video_catalog() -> str:
    return (
        f"- {FRAME_VIDEO_NAME}：制帧叙事规划，逐帧故事、静态画面与独立视频描述；不是视频渲染规范。"
    )


def frame_video_render() -> str:
    path = _skill_dir(FRAME_VIDEO_NAME) / "SKILL.md"
    if not path.is_file():
        raise AppError(
            ErrorCode.INTERNAL,
            "制帧规划技能缺失",
            f"未找到 {FRAME_VIDEO_NAME}/SKILL.md。",
            ["检查安装包是否包含 skill 目录后重试"],
        )
    return path.read_text(encoding="utf-8")

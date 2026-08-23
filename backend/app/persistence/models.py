"""ORM 模型。

每个工程一个 project.db，因此这里的表都属于「单工程」范围；
project 表只会有一行——它就是这个工程自己的清单在数据库里的镜像。

字段口径见 docs/03-数据模型与接口契约.md §2.1。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.db import Base


def utc_now() -> str:
    """统一时间口径：UTC ISO8601 字符串，前端负责本地化显示。"""
    return datetime.now(UTC).isoformat()


class Project(Base):
    __tablename__ = "project"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    synopsis: Mapped[str | None] = mapped_column(Text)
    cover_asset_id: Mapped[str | None] = mapped_column(String(40))
    style_preset: Mapped[str | None] = mapped_column(String(100))

    width: Mapped[int] = mapped_column(Integer, nullable=False, default=1920)
    height: Mapped[int] = mapped_column(Integer, nullable=False, default=1080)
    fps: Mapped[float] = mapped_column(Float, nullable=False, default=25.0)
    aspect_ratio: Mapped[str | None] = mapped_column(String(20))
    # 时长单位：frames 便于与 ComfyUI 的帧数参数对齐，seconds 便于人读。
    duration_unit: Mapped[str] = mapped_column(String(10), nullable=False, default="frames")

    default_video_workflow_id: Mapped[str | None] = mapped_column(String(40))
    default_image_workflow_id: Mapped[str | None] = mapped_column(String(40))
    default_first_last_workflow_id: Mapped[str | None] = mapped_column(String(40))
    default_upscale_workflow_id: Mapped[str | None] = mapped_column(String(40))
    #: 项目级生成方式：工作流资源在应用层维护，项目只选择采用哪条调用路径。
    generation_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="comfy_preset")
    #: 项目唯一生成预设；预设由应用级管理，项目只选择其中一份。
    preset_name: Mapped[str | None] = mapped_column(String(100))
    #: 普通 Shot 的 R2V 预设；为空时回退到旧的 preset_name。
    r2v_preset_name: Mapped[str | None] = mapped_column(String(100))
    #: 首尾帧 / FL2VA 衔接预设；为空时回退到旧的 preset_name。
    flf_preset_name: Mapped[str | None] = mapped_column(String(100))
    default_prompt_style: Mapped[str | None] = mapped_column(Text)
    negative_prompt: Mapped[str | None] = mapped_column(Text)

    # 写库时的 schema 版本；打开工程时与 settings.schema_version 比对决定是否迁移。
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)

    created_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utc_now)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utc_now)

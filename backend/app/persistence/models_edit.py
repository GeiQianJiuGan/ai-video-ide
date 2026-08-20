"""时间线与导出。

时间线是确定性的编辑系统：它只引用 GenerationVersion，不调用任何 AI。
即使 ComfyUI 和 LLM 全都不在，这一层也必须能装配、剪辑、导出。
"""

from __future__ import annotations

from sqlalchemy import Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.db import Base
from app.persistence.models import utc_now

TRACK_KINDS = ("video", "audio", "subtitle")
TRANSITION_KINDS = ("cut", "dissolve", "fade_in", "fade_out")


class Timeline(Base):
    __tablename__ = "timeline"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, default="主时间线")
    fps: Mapped[float] = mapped_column(Float, nullable=False, default=25.0)
    width: Mapped[int] = mapped_column(Integer, nullable=False, default=1920)
    height: Mapped[int] = mapped_column(Integer, nullable=False, default=1080)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utc_now)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utc_now)


class Track(Base):
    __tablename__ = "track"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    timeline_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("timeline.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="video")
    index_no: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    name: Mapped[str] = mapped_column(String(100), nullable=False, default="V1")
    muted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class TimelineClip(Base):
    """轨道上的一个片段。start / duration 单位为秒，导出时换算成 FFmpeg 参数。"""

    __tablename__ = "timeline_clip"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    track_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("track.id", ondelete="CASCADE"), nullable=False, index=True
    )
    shot_id: Mapped[str | None] = mapped_column(String(40), index=True)
    version_id: Mapped[str | None] = mapped_column(String(40))
    asset_id: Mapped[str | None] = mapped_column(String(40))
    index_no: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    start: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    duration: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    #: 源素材裁切点（秒）
    in_point: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    out_point: Mapped[float | None] = mapped_column(Float)
    label: Mapped[str | None] = mapped_column(String(200))


class Transition(Base):
    __tablename__ = "transition"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    timeline_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("timeline.id", ondelete="CASCADE"), nullable=False, index=True
    )
    from_clip_id: Mapped[str | None] = mapped_column(String(40))
    to_clip_id: Mapped[str | None] = mapped_column(String(40))
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="dissolve")
    duration: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)


class ExportRecord(Base):
    """一次导出。记下由哪些版本组成，成片来源可追溯。"""

    __tablename__ = "export_record"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    timeline_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    #: 参与本次导出的 GenerationVersion id 列表（JSON）
    version_ids_json: Mapped[str | None] = mapped_column(Text)
    command: Mapped[str | None] = mapped_column(Text)
    error_json: Mapped[str | None] = mapped_column(Text)
    duration: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utc_now)
    finished_at: Mapped[str | None] = mapped_column(String(40))

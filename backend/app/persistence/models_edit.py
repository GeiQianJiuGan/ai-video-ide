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
#: 片段是谁铺的。**装配只碰 `assembled`**，`manual` 的一律不动——理由见 TimelineClip.origin。
CLIP_ORIGINS = ("assembled", "manual")


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
    #: 这一段不出声（0/1）。**把声音拆到音频轨之后源片段会被置 1**——声音已经挪出去了，
    #: 再让画面自己也出一遍就会听见两次。整条轨道的静音是 `Track.muted`，两者独立生效。
    muted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: 音量倍数（1 = 原样）。音频轨叠加时靠它配比例，导出时变成 filter 里的 volume。
    volume: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    #: 这一段音频是从哪个片段拆出来的（只有音频轨上的片段有值）。刻意不加外键：
    #: 源片段被删掉或重新装配之后它照旧能播，只是不再知道出处。
    source_clip_id: Mapped[str | None] = mapped_column(String(40), index=True)
    #: **谁铺的这一段**（`assembled` / `manual`）。装配只更新与删除 `assembled`；
    #: 用户自己加的、从长视频切出来的、拆到音频轨上的声音一律 `manual`，装配永不触碰。
    #:
    #: 没有这一列的时候 `auto_assemble` 只能清空整条视频轨再重建，于是：手工裁切 / 静音 /
    #: 音量全丢，被拆出去的声音因为源片段 id 消失而集体悬空（`_shape` 里那个
    #: `source_missing`）。所以「重新装配」曾经是个不敢按的按钮——这一列 + 对位调和
    #: （`_assemble_diff`）就是为了让它变成一次可预期的操作。
    origin: Mapped[str] = mapped_column(String(20), nullable=False, default="manual")


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

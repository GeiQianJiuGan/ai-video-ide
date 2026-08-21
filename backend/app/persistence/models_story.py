"""剧本、Scene 与 Shot。

Shot 是整个系统的中心：它引用地点变体与出场形象，产出 GenerationVersion，
最终成为时间线上的一个片段。status 只描述「创作进度」，不描述任务状态——
任务状态在 job 表里，两者混淆会让人看不懂界面。
"""

from __future__ import annotations

from sqlalchemy import Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.db import Base
from app.persistence.models import utc_now

#: Shot 创作进度。draft 缺信息，ready 可生成，generated 有当前版本，review 待审，locked 已定稿。
SHOT_STATUS = ("draft", "ready", "generated", "review", "locked")


class Story(Base):
    """剧本原文与拆解模式。一个工程一份，段落与 Scene 双向对应。"""

    __tablename__ = "story"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="未命名剧本")
    raw_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: manual / ai_assisted / ai_auto——记录这份结构是怎么来的，便于回溯。
    mode: Mapped[str] = mapped_column(String(20), nullable=False, default="manual")
    created_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utc_now)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utc_now)


class Scene(Base):
    __tablename__ = "scene"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    index_no: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    #: 剧本原文里对应的段落，用于双向定位
    source_text: Mapped[str | None] = mapped_column(Text)
    location_variant_id: Mapped[str | None] = mapped_column(
        String(40), ForeignKey("location_variant.id", ondelete="SET NULL")
    )
    time_of_day: Mapped[str | None] = mapped_column(String(50))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utc_now)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utc_now)


class Shot(Base):
    __tablename__ = "shot"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    scene_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("scene.id", ondelete="CASCADE"), nullable=False, index=True
    )
    index_no: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    description: Mapped[str | None] = mapped_column(Text)
    #: shot（导演排的戏）/ transition（两幕之间那段 1~2s 的转场，由 SceneLink 生成）。
    #: 见 `persistence/models_flow.py::SHOT_KINDS`——转场镜头也是正常镜头，一样有版本、
    #: 一样进时间线，只是它不是人排出来的，界面上要能区分。
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="shot")

    #: 时长，单位由工程的 duration_unit 决定（frames 或 seconds）
    duration: Mapped[float] = mapped_column(Float, nullable=False, default=4.0)
    camera: Mapped[str | None] = mapped_column(String(50))
    movement: Mapped[str | None] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")

    prompt: Mapped[str | None] = mapped_column(Text)
    negative_prompt: Mapped[str | None] = mapped_column(Text)
    seed: Mapped[int | None] = mapped_column(Integer)
    steps: Mapped[int | None] = mapped_column(Integer)
    workflow_id: Mapped[str | None] = mapped_column(String(40))

    #: 上游镜头：需要它的末帧做首帧时填。空表示不依赖任何镜头。
    prev_shot_id: Mapped[str | None] = mapped_column(String(40))
    current_version_id: Mapped[str | None] = mapped_column(String(40))
    #: Context Inspector 里的人工干预记录（移除/添加/替换），JSON 列表
    context_overrides_json: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utc_now)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utc_now)


class ShotCast(Base):
    """镜头出场表：哪个形象出现在这个镜头里。"""

    __tablename__ = "shot_cast"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    shot_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("shot.id", ondelete="CASCADE"), nullable=False, index=True
    )
    appearance_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("appearance.id", ondelete="CASCADE"), nullable=False
    )
    note: Mapped[str | None] = mapped_column(Text)


class ShotProp(Base):
    """镜头道具表：连续性检查依赖它判断「伞什么时候还在」。"""

    __tablename__ = "shot_prop"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    shot_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("shot.id", ondelete="CASCADE"), nullable=False, index=True
    )
    prop_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("prop.id", ondelete="CASCADE"), nullable=False
    )
    #: present（出现）/ discarded（被丢弃）——用于前后矛盾检查
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="present")

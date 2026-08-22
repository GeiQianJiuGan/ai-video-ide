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
    """一幕。流程图上的一个节点，也是「小节点」（prompt / 人物 / 地点）的挂载点。

    `prompt` 是这一幕的必填件——镜头没写自己的 prompt 时由它兜底（见
    `services/context.py` 与 `services/generation.py::enqueue_shot`）。
    `location_variant_id` 保留为「主地点」：多选地点存在 `scene_location` 里，
    第一条会同步回这一列，于是 Context Resolver 与分镜板一行都不用改。
    """

    __tablename__ = "scene"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    index_no: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    #: 剧本原文里对应的段落，用于双向定位
    source_text: Mapped[str | None] = mapped_column(Text)
    #: 这一幕的 prompt（小节点里唯一必填的那个）。镜头级 prompt 优先，空着才用它。
    prompt: Mapped[str | None] = mapped_column(Text)
    #: 主地点变体。多选地点在 scene_location 表里，这一列始终等于其中第一条。
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
    #: **这个镜头采用了哪一段成片**。全工程只有这一个「用哪一段」的指针：时间线装配
    #: （`services/timeline.py::auto_assemble`）、下游镜头抽末帧、流程图节点上播的那一段
    #: 都读它。刻意不加外键（版本表反过来引用镜头，加外键会绕成一圈），
    #: 取不到时按「这个镜头还没出片」处理。
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


class SceneCast(Base):
    """幕出场表：这一幕里有哪些形象（流程图上的「人物」小节点）。

    镜头没挂自己的 `ShotCast` 时由它兜底——小节点必须真的影响生成，否则只是装饰。
    `index_no` 是显示与优先级顺序，替换时整表重写（与 `set_shot_cast` 同一套做法）。
    """

    __tablename__ = "scene_cast"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    scene_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("scene.id", ondelete="CASCADE"), nullable=False, index=True
    )
    appearance_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("appearance.id", ondelete="CASCADE"), nullable=False
    )
    index_no: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    note: Mapped[str | None] = mapped_column(Text)


class SceneLocation(Base):
    """幕地点表：这一幕可以用哪几个地点变体（流程图上的「场景」小节点）。

    `index_no == 0` 的那条是主地点，会同步进 `Scene.location_variant_id`；
    其余几条在 Context Resolver 里也算「可用」，只是优先级低一档。
    """

    __tablename__ = "scene_location"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    scene_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("scene.id", ondelete="CASCADE"), nullable=False, index=True
    )
    location_variant_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("location_variant.id", ondelete="CASCADE"), nullable=False
    )
    index_no: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    note: Mapped[str | None] = mapped_column(Text)

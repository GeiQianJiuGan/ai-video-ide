"""幕流程图：场景之间的衔接，以及 AI 导演的会话记录。

两张表，各自解决一件在别处放不下的事：

  - **SceneLink** 让「衔接」成为一等公民。以前两幕之间怎么接是隐含的（要么硬切，
    要么靠某个镜头的 `prev_shot_id`），没有地方能问「第 2 幕到第 3 幕是怎么接的」。
    现在能问，而三种模式落到已有机制上都不新造概念：
      * `cut` —— 什么都不生成；
      * `transition` —— 生成一段 1~2s 的转场，落成 `Shot.kind="transition"` 的镜头，
        **挂在 from_scene 下、index_no 排最后**，于是 `timeline.auto_assemble` 的
        「按 scene.index_no + shot.index_no」排序天然把它放在两幕之间，导出一行不用改；
      * `tail_frame` —— 只是把下游首镜头的 `prev_shot_id` 指到上游末镜头，
        复用已有的 `depends_on` / `wait_reason`，「等上游末帧」照旧是可解释的等待。

  - **ShotLink** 是同一件事在**镜头级**的版本：同一幕内相邻两个镜头之间怎么接
    （`cut` 无转场 / `transition` 补一段短转场）。没有行就等于 `cut`。
    为什么需要它见类的 docstring——简单说是「能引设定图的模型做不了严格首尾帧」。

  - **DirectorTurn** 是 AI 协作栏的对话与提案。提案必须能刷新页面后还在——
    否则用户审阅到一半刷新就白干了。落库的是**提案**，不是改动本身：
    写库要等用户逐条采用（见 `services/director.py`）。
"""

from __future__ import annotations

from sqlalchemy import Float, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.db import Base
from app.persistence.models import utc_now

#: 衔接方式。改这个元组要同时改 `services/sequence.py::LINK_HINT`，
#: 那里的中文说明会直接显示给用户。
LINK_MODES = ("cut", "transition", "tail_frame")

#: 镜头之间的衔接方式。**没有 `tail_frame`**：镜头级的「续接末帧」早就有表达方式了
#: （`Shot.prev_shot_id`），再给它一个同义词只会让两处配置打架。
#: 改这个元组要同时改 `services/sequence.py::SHOT_LINK_HINT`。
SHOT_LINK_MODES = ("cut", "transition")

#: 镜头种类。`transition` 的镜头不是导演排的戏，是衔接生成出来的一段；
#: `ingested` 是从一段成片切出来的（画面已经有了，`GenerationVersion` 带 in/out 区间）。
#: 分镜板与流程图要能把这三种区分开。**它不参与参数解析**（共用与否只看镜头上那一项
#: 空不空，见 `services/params.py`），只用来画界面与决定要不要过上下文门槛。
SHOT_KINDS = ("shot", "transition", "ingested")


class SceneLink(Base):
    """一条「这一幕接下一幕」的边。同一对场景之间只允许一条。"""

    __tablename__ = "scene_link"
    __table_args__ = (UniqueConstraint("from_scene_id", "to_scene_id", name="uq_scene_link_pair"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    from_scene_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("scene.id", ondelete="CASCADE"), nullable=False, index=True
    )
    to_scene_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("scene.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: cut / transition / tail_frame，见 LINK_MODES
    mode: Mapped[str] = mapped_column(String(20), nullable=False, default="cut")
    #: transition 模式下生成出来的那个镜头。不是外键：镜头被删掉时这里只是变成一条
    #: 「还没生成」的线索，而不该把整条衔接一起带走。
    shot_id: Mapped[str | None] = mapped_column(String(40))
    #: 转场时长，单位秒。只有 transition 用得上。
    duration: Mapped[float] = mapped_column(Float, nullable=False, default=1.5)
    #: 转场的画面描述（「雨越下越大，镜头拉远」）。留空时由两幕的标题拼一句。
    prompt: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utc_now)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utc_now)


class ShotLink(Base):
    """一条「这个镜头接下一个镜头」的边（同一幕内相邻的两个正片镜头之间）。

    为什么镜头之间也要这张表：有些视频模型能做 R2V（参考设定图）但做不了严格首尾帧，
    反过来能严格首尾帧的又收不了设定图——于是两个镜头之间画面对不上。这时的出路是
    在两者之间插一段短转场视频，把「上一镜真末帧 → 下一镜真首帧」这一格补出来。

    落地方式和 `SceneLink` 完全一样，不新造概念：`transition` 生成一个
    `Shot.kind="transition"` 的镜头，挂在 from_shot 所在的那一幕、**紧跟在 from_shot 之后**，
    于是 `timeline.auto_assemble` 的「scene.index_no + shot.index_no」排序天然把它放在
    两个镜头之间，导出侧一行都不用改。

    没有行 = `cut`（无转场，两镜直接硬切）——这正是本表出现之前的行为，
    所以老工程升上来什么都不会变。
    """

    __tablename__ = "shot_link"
    __table_args__ = (UniqueConstraint("from_shot_id", "to_shot_id", name="uq_shot_link_pair"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    from_shot_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("shot.id", ondelete="CASCADE"), nullable=False, index=True
    )
    to_shot_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("shot.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: cut / transition，见 SHOT_LINK_MODES
    mode: Mapped[str] = mapped_column(String(20), nullable=False, default="cut")
    #: transition 模式下生成出来的那个镜头。同 SceneLink.shot_id：**不是外键**，
    #: 镜头被删掉时这里只是退回「还没生成」，不该把整条衔接一起带走。
    shot_id: Mapped[str | None] = mapped_column(String(40))
    #: 转场时长，单位秒。只有 transition 用得上。
    duration: Mapped[float] = mapped_column(Float, nullable=False, default=1.5)
    #: 转场的画面描述。留空时由两个镜头的标题拼一句。
    prompt: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utc_now)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utc_now)


class DirectorTurn(Base):
    """AI 导演的一轮对话或一份提案。只增不改，刷新页面不丢。"""

    __tablename__ = "director_turn"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    #: user / assistant / proposal / applied——proposal 那条的 content_json 里是 ops 数组
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="user")
    content_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utc_now)

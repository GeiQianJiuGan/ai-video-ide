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

#: 镜头种类。`transition` 的镜头不是导演排的戏，是衔接生成出来的一段，
#: 分镜板与流程图要能把它和正片镜头区分开。
SHOT_KINDS = ("shot", "transition")


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


class DirectorTurn(Base):
    """AI 导演的一轮对话或一份提案。只增不改，刷新页面不丢。"""

    __tablename__ = "director_turn"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    #: user / assistant / proposal / applied——proposal 那条的 content_json 里是 ops 数组
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="user")
    content_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utc_now)

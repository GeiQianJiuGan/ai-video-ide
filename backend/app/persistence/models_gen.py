"""Workflow（能力层）、生成版本与任务队列。

业务层只认 capability（text2image / image2video / first_last_frame / upscale），
永远不认模型名字——换模型只换 workflow 行，Shot 一个字段都不用改。

GenerationVersion 只增不改：每次生成冻结当次的全部参数与上下文快照。
"""

from __future__ import annotations

from sqlalchemy import Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.db import Base
from app.persistence.models import utc_now

CAPABILITIES = ("text2image", "image2video", "first_last_frame", "upscale")
#: 每种能力必须绑定的输入槽；缺一个就不能标为就绪。
REQUIRED_SLOTS: dict[str, tuple[str, ...]] = {
    "text2image": ("prompt",),
    "image2video": ("prompt", "reference_image"),
    "first_last_frame": ("first_frame", "last_frame"),
    "upscale": ("source_image",),
}

#: **任务 kind 的唯一一张表。** 以前这些字符串散在 `services/generation.py` 的
#: `{"first_last_frame", "transition", "fl2va"}` 之类的集合里，`CAPABILITIES` 却只列了四个，
#: 于是「有哪些 kind」在两处对不上。加音频与二次处理之前先把它收成一处。
#:
#: 三族，语义完全不同：
#:   · 出画面（VIDEO_KINDS）——从首帧 / 首尾帧生成一段新画面；
#:   · 二次处理（REFINE_KINDS）——**输入是已经出好的那一版**，产出同一个镜头上的新版本，
#:     `parent_version_id` 记血缘。画面重跑一次很贵，能只处理就别重生成；
#:   · 出声音（AUDIO_KINDS）——产出 `kind="audio"` 的版本，落 `current_audio_version_id`。
VIDEO_KINDS = ("image2video", "first_last_frame", "transition", "fl2va")
REFINE_KINDS = ("upscale", "interpolate", "recut")
AUDIO_KINDS = ("audio",)
JOB_KINDS = (*VIDEO_KINDS, *REFINE_KINDS, *AUDIO_KINDS)
#: 这些 kind 要用上游镜头的末帧当首帧（严格首尾帧那条路）。判定只放这一份。
NEEDS_LAST_FRAME = ("first_last_frame", "transition", "fl2va")
#: 产物是音频的 kind。
AUDIO_OUTPUT_KINDS = AUDIO_KINDS
JOB_STATUS = ("queued", "waiting", "running", "done", "failed", "canceled", "paused")
#: 版本产物的媒体类型。`video` 是画面那一版，`audio` 是声音那一版，`image` 是出图。
VERSION_KINDS = ("video", "audio", "image")


class Workflow(Base):
    __tablename__ = "workflow"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    capability: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    #: workflow_api.json 原文，整份存下来：换机器也能重放
    api_json: Mapped[str] = mapped_column(Text, nullable=False)
    #: 槽位 → "节点id.字段名"，例如 {"prompt": "6.text"}
    bindings_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    #: 解析出的节点摘要，供前端画节点列表
    nodes_json: Mapped[str | None] = mapped_column(Text)
    #: 用到的自定义节点 class_type 列表
    required_nodes_json: Mapped[str | None] = mapped_column(Text)
    #: draft（未校验）/ ready（校验通过）/ invalid（校验失败）/ disabled（人工停用）
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    validation_json: Mapped[str | None] = mapped_column(Text)
    is_default: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utc_now)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utc_now)


class GenerationVersion(Base):
    __tablename__ = "generation_version"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    shot_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("shot.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="video")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="done")
    asset_id: Mapped[str | None] = mapped_column(String(40))
    workflow_id: Mapped[str | None] = mapped_column(String(40))
    #: 冻结的参数（prompt / seed / steps / 分辨率 / 时长）
    params_json: Mapped[str | None] = mapped_column(Text)
    #: 冻结的上下文账单：这次到底喂了什么进去
    context_json: Mapped[str | None] = mapped_column(Text)
    error_json: Mapped[str | None] = mapped_column(Text)
    duration: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="generated")
    #: **这一版是从哪一版处理出来的**（超分 / 插帧 / 换音频 / 重做尾段）。
    #: 二次处理不另建一套体系：它产出的仍然是同一个镜头上的一个新版本，于是硬约束 3
    #: （只增不改、随时回退到未处理那一版）、采用入口、装配、队列全都一行不用改。
    #: 有了这一列，版本轨能画出「原始 v1 → 超分 v2 → 换音频 v3」这条谱系，
    #: 而不是三条互不相干的版本。不加外键：父版本不在了按「不知道出处」处理。
    parent_version_id: Mapped[str | None] = mapped_column(String(40))
    #: **这一版只用源文件的某一段**（秒）。两列都空 = 整个文件，所以老版本行为不变。
    #: 长视频切段靠它：一幕下面 N 个镜头各挂一版，`asset_id` 全部指向同一个源文件，
    #: 各自带自己的区间——零文件复制。装配时抄进 `TimelineClip.in_point` / `out_point`。
    in_point: Mapped[float | None] = mapped_column(Float)
    out_point: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utc_now)


class Job(Base):
    """生成任务。depends_on 让「等上游末帧」变成可解释的等待，而不是卡住。"""

    __tablename__ = "job"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    shot_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("shot.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(30), nullable=False, default="image2video")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued", index=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    progress: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    depends_on: Mapped[str | None] = mapped_column(String(40))
    #: 为什么现在不动——「等待上游 Shot 14 完成（需要末帧）」这句话的来源
    wait_reason: Mapped[str | None] = mapped_column(Text)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    workflow_id: Mapped[str | None] = mapped_column(String(40))
    version_id: Mapped[str | None] = mapped_column(String(40))
    error_json: Mapped[str | None] = mapped_column(Text)
    params_json: Mapped[str | None] = mapped_column(Text)
    #: **这条任务属于哪一次编排**（一次「单线程续接」/「并发生成」/「整幕配音」…）。
    #: 一次编排会一口气入队几十条任务，队列里逐条平铺时用户根本看不出「这是我刚才点的
    #: 那一下」；有了这一列，界面把它们合并成**一条可展开的任务**，进度是「第 N/M 步」
    #: 而不是一个假的百分比（ComfyUI 不回显进度，画进度条就是在编）。
    #:
    #: 三条刻意的规矩：
    #:   · **空值是常态**——单个镜头的生成不属于任何编排，界面照旧一行一条；
    #:   · **不加外键、不建 batch 表**——一次编排没有任何独立于任务的状态，
    #:     全部能从成员任务算出来（总数 / 走到第几步 / 失败在哪一条），
    #:     另立一张表只会多一份可能对不上的真相；
    #:   · **失败了不清空这一列**——重跑整批就是靠它把成员找回来的。
    batch_id: Mapped[str | None] = mapped_column(String(40), index=True)
    #: 这一批在界面上叫什么（「单线程续接 · 12 个镜头」）。入队那一刻就定死，
    #: 事后镜头改名也不改它——它说的是「你当时点的那一下」。
    batch_label: Mapped[str | None] = mapped_column(String(200))
    #: 这一批是哪一种编排：sequential / parallel / scene / transition / dub / refine。
    #: 只用于文案与图标，**业务逻辑一律不按它分支**。
    batch_kind: Mapped[str | None] = mapped_column(String(30))
    #: 在这一批里排第几（从 1 起）。单线程续接靠它说「执行到第 3/12 步」。
    batch_seq: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utc_now)
    started_at: Mapped[str | None] = mapped_column(String(40))
    finished_at: Mapped[str | None] = mapped_column(String(40))

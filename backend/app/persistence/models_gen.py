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
JOB_STATUS = ("queued", "waiting", "running", "done", "failed", "canceled", "paused")


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
    created_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utc_now)
    started_at: Mapped[str | None] = mapped_column(String(40))
    finished_at: Mapped[str | None] = mapped_column(String(40))

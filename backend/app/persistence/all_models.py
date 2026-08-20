"""全部 ORM 模型的汇总导入点。

Base.metadata 只有在模型模块被 import 之后才完整，因此 Alembic env 与测试
统一 import 这个模块，而不是逐个 import——漏一个就会漏一张表。
"""

from __future__ import annotations

from app.persistence.models import Project, utc_now
from app.persistence.models_cast import INHERITABLE, Appearance, Character, SheetVersion
from app.persistence.models_edit import (
    ExportRecord,
    Timeline,
    TimelineClip,
    Track,
    Transition,
)
from app.persistence.models_gen import (
    CAPABILITIES,
    JOB_STATUS,
    REQUIRED_SLOTS,
    GenerationVersion,
    Job,
    Workflow,
)
from app.persistence.models_story import (
    SHOT_STATUS,
    Scene,
    Shot,
    ShotCast,
    ShotProp,
    Story,
)
from app.persistence.models_world import (
    Asset,
    AssetRef,
    Location,
    LocationReference,
    LocationVariant,
    Prop,
    PropReference,
)

__all__ = [
    "CAPABILITIES",
    "INHERITABLE",
    "JOB_STATUS",
    "REQUIRED_SLOTS",
    "SHOT_STATUS",
    "Appearance",
    "Asset",
    "AssetRef",
    "Character",
    "ExportRecord",
    "GenerationVersion",
    "Job",
    "Location",
    "LocationReference",
    "LocationVariant",
    "Project",
    "Prop",
    "PropReference",
    "Scene",
    "SheetVersion",
    "Shot",
    "ShotCast",
    "ShotProp",
    "Story",
    "Timeline",
    "TimelineClip",
    "Track",
    "Transition",
    "Workflow",
    "utc_now",
]

"""角色与形象。

两级结构（docs/03 §2.2）：
  Character   这个人是谁——名字、性格、背景，一个项目里唯一
  Appearance  这个人在某个阶段长什么样——可从父形象派生

派生的规则写在这里，是整个系统「值从哪来」可解释性的第一处：
face / hair / body / traits 默认继承父形象，age / costume / state 通常覆写。
一个字段是自己填的还是继承来的，由 overrides 列表决定，不靠「值是否为空」猜。
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.db import Base
from app.persistence.models import utc_now

#: 可继承字段：派生形象未覆写时，值取自父形象。
INHERITABLE = ("face", "hair", "body", "traits", "costume", "state", "age", "notes")


class Character(Base):
    __tablename__ = "character"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    alias: Mapped[str | None] = mapped_column(String(200))
    gender: Mapped[str | None] = mapped_column(String(20))
    age_range: Mapped[str | None] = mapped_column(String(50))
    personality: Mapped[str | None] = mapped_column(Text)
    background: Mapped[str | None] = mapped_column(Text)
    voice_desc: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utc_now)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utc_now)


class Appearance(Base):
    __tablename__ = "appearance"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    character_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("character.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: 派生来源；None 表示这是一个根形象。
    parent_id: Mapped[str | None] = mapped_column(
        String(40), ForeignKey("appearance.id", ondelete="SET NULL")
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)

    face: Mapped[str | None] = mapped_column(Text)
    hair: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text)
    traits: Mapped[str | None] = mapped_column(Text)
    age: Mapped[str | None] = mapped_column(String(50))
    costume: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str | None] = mapped_column(String(100))
    notes: Mapped[str | None] = mapped_column(Text)

    #: 已覆写的字段名，逗号分隔。不在其中的字段一律显示为继承值。
    overrides: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: 镜头没指定形象时用哪个；每个角色最多一个。
    is_default: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utc_now)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utc_now)


class SheetVersion(Base):
    """Character Sheet：某个形象的定妆图。永不覆盖，只增版本。"""

    __tablename__ = "sheet_version"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    appearance_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("appearance.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    asset_id: Mapped[str | None] = mapped_column(String(40))
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="manual")
    params_json: Mapped[str | None] = mapped_column(Text)
    is_current: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utc_now)

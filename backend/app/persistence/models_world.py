"""场景、道具与资产总账。

Asset 是所有落盘文件的唯一登记处；AssetRef 记录「谁在用它」。
有了这两张表，孤儿检测就是一次左连接，而不是满硬盘扫描猜测。
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.db import Base
from app.persistence.models import utc_now


class Location(Base):
    __tablename__ = "location"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    #: 从应用级素材库采用而来时记下库里那行的 id，只是出处（见 services/adopt.py）。
    origin_library_id: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utc_now)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utc_now)


class LocationVariant(Base):
    """同一地点的不同时间/天气/光线，例如「城南旧宅 · 雨夜」。"""

    __tablename__ = "location_variant"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    location_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("location.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    time_of_day: Mapped[str | None] = mapped_column(String(50))
    weather: Mapped[str | None] = mapped_column(String(50))
    lighting: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utc_now)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utc_now)


class LocationReference(Base):
    """变体的参考图，可按机位区分。"""

    __tablename__ = "location_reference"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    variant_id: Mapped[str] = mapped_column(
        String(40),
        ForeignKey("location_variant.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    asset_id: Mapped[str] = mapped_column(String(40), nullable=False)
    camera: Mapped[str | None] = mapped_column(String(50))
    note: Mapped[str | None] = mapped_column(Text)
    is_current: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utc_now)


class Prop(Base):
    __tablename__ = "prop"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    #: 从应用级素材库采用而来时记下库里那行的 id，只是出处（见 services/adopt.py）。
    origin_library_id: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utc_now)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utc_now)


class PropReference(Base):
    __tablename__ = "prop_reference"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    prop_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("prop.id", ondelete="CASCADE"), nullable=False, index=True
    )
    asset_id: Mapped[str] = mapped_column(String(40), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    note: Mapped[str | None] = mapped_column(Text)
    is_current: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utc_now)


class Asset(Base):
    """落盘文件总账。path 相对工程目录存储，整个目录拷走后依然有效。"""

    __tablename__ = "asset"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    kind: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    mime: Mapped[str | None] = mapped_column(String(100))
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    duration: Mapped[float | None] = mapped_column(Integer)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sha1: Mapped[str | None] = mapped_column(String(40), index=True)
    #: manual（手动上传）/ generated（本系统生成）/ imported（外部导入）
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="manual")
    meta_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utc_now)


class AssetRef(Base):
    """资产的反向引用：owner_kind + owner_id 用了 asset_id。"""

    __tablename__ = "asset_ref"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    asset_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("asset.id", ondelete="CASCADE"), nullable=False, index=True
    )
    owner_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    role: Mapped[str | None] = mapped_column(String(30))
    created_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utc_now)

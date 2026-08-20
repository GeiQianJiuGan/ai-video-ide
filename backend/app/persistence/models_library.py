"""应用级素材库的表结构（Phase 3）。

为什么单独一套 Base：素材库不是工程——它有自己的目录与 library.db，而
`alembic/env.py` 的 `target_metadata` 是工程侧的 `Base.metadata`。库表挂在 `Base`
上会被 autogenerate 写进工程迁移，于是每个 project.db 里都会多出一堆用不到的表。
所以这里另立 `LibraryBase`，两套 metadata 各建各的表。

表结构刻意镜像工程侧素材层（`models_world` / `models_cast`），列名一一对应：
「采用」就是把库里的行按同名字段喂给工程侧**已有**的写路径，不需要字段翻译。

库不走 alembic：打开时 `create_all`（幂等、只增表）+ 清单里的 `schema_version`
把关。将来要改列时再加 `alembic_library/` 分支——这个取舍写在 CLAUDE.md 里。
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.persistence.models import utc_now


class LibraryBase(DeclarativeBase):
    """素材库表的基类。刻意与工程侧的 Base 分开，见模块 docstring。"""


class LibraryMeta(LibraryBase):
    """库自身的身份行，作用等同工程里的 project 表：只会有一行。"""

    __tablename__ = "library_meta"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utc_now)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utc_now)


class LibAsset(LibraryBase):
    """库内落盘文件总账。path 相对**库目录**存，库整体拷走仍然有效。"""

    __tablename__ = "library_asset"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    kind: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    mime: Mapped[str | None] = mapped_column(String(100))
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    duration: Mapped[float | None] = mapped_column(Integer)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sha1: Mapped[str | None] = mapped_column(String(40), index=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="manual")
    #: 人看的名字。工程侧靠 meta_json.filename 就够了，库是长期资产，值得一个可改的标题
    title: Mapped[str | None] = mapped_column(String(200))
    note: Mapped[str | None] = mapped_column(Text)
    meta_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utc_now)


class LibAssetRef(LibraryBase):
    """库内的反向引用：哪个预设在用这个素材。删素材前靠它说清会破坏什么。"""

    __tablename__ = "library_asset_ref"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    asset_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("library_asset.id", ondelete="CASCADE"), nullable=False, index=True
    )
    owner_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    role: Mapped[str | None] = mapped_column(String(30))
    created_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utc_now)


class LibCharacter(LibraryBase):
    """角色预设。列与工程侧 character 同名，采用时直接喂给 cast.create_character。"""

    __tablename__ = "library_character"

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


class LibAppearance(LibraryBase):
    """形象预设。

    parent_id / overrides 与工程侧同构地保留下来：库里现在只建平铺形象，
    但采用时要能把继承关系原样搬进工程，缺了这两列就搬不了。
    """

    __tablename__ = "library_appearance"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    character_id: Mapped[str] = mapped_column(
        String(40),
        ForeignKey("library_character.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    parent_id: Mapped[str | None] = mapped_column(
        String(40), ForeignKey("library_appearance.id", ondelete="SET NULL")
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

    overrides: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_default: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utc_now)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utc_now)


class LibSheet(LibraryBase):
    """形象预设的定妆图。与工程侧 sheet_version 一样只增版本，不覆盖。"""

    __tablename__ = "library_sheet"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    appearance_id: Mapped[str] = mapped_column(
        String(40),
        ForeignKey("library_appearance.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    asset_id: Mapped[str | None] = mapped_column(String(40))
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="manual")
    is_current: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utc_now)


class LibLocation(LibraryBase):
    __tablename__ = "library_location"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utc_now)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utc_now)


class LibLocationVariant(LibraryBase):
    """地点预设的变体（「雨夜」「清晨」）。镜头引用的是变体，所以库里也必须有它。"""

    __tablename__ = "library_location_variant"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    location_id: Mapped[str] = mapped_column(
        String(40),
        ForeignKey("library_location.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    time_of_day: Mapped[str | None] = mapped_column(String(50))
    weather: Mapped[str | None] = mapped_column(String(50))
    lighting: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utc_now)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utc_now)


class LibLocationReference(LibraryBase):
    __tablename__ = "library_location_reference"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    variant_id: Mapped[str] = mapped_column(
        String(40),
        ForeignKey("library_location_variant.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    asset_id: Mapped[str] = mapped_column(String(40), nullable=False)
    camera: Mapped[str | None] = mapped_column(String(50))
    note: Mapped[str | None] = mapped_column(Text)
    is_current: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utc_now)


class LibProp(LibraryBase):
    __tablename__ = "library_prop"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utc_now)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utc_now)


class LibPropReference(LibraryBase):
    __tablename__ = "library_prop_reference"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    prop_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("library_prop.id", ondelete="CASCADE"), nullable=False, index=True
    )
    asset_id: Mapped[str] = mapped_column(String(40), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    note: Mapped[str | None] = mapped_column(Text)
    is_current: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utc_now)


class LibTag(LibraryBase):
    """标签。库会越攒越大，没有标签就只能靠肉眼翻——这是库相对工程多出来的一层。"""

    __tablename__ = "library_tag"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(60), nullable=False, unique=True)
    color: Mapped[str | None] = mapped_column(String(20))
    created_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utc_now)


class LibTagLink(LibraryBase):
    """标签挂在谁身上。owner_kind = asset / character / location / prop。"""

    __tablename__ = "library_tag_link"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tag_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("library_tag.id", ondelete="CASCADE"), nullable=False, index=True
    )
    owner_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utc_now)

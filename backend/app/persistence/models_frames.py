"""Independent frame-authoring drafts; legacy Shot inputs are never mutated."""

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.db import Base
from app.persistence.models import utc_now


class FrameWorkspace(Base):
    __tablename__ = "frame_workspace"
    shot_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("shot.id", ondelete="CASCADE"), primary_key=True
    )
    video_json: Mapped[str] = mapped_column(Text, default="{}")


class FrameCard(Base):
    __tablename__ = "frame_card"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    shot_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("shot.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer, default=0)
    title: Mapped[str] = mapped_column(String(200))
    prompt: Mapped[str] = mapped_column(Text, default="")
    story: Mapped[str] = mapped_column(Text, default="")
    role: Mapped[str] = mapped_column(String(20), default="key")
    refs_json: Mapped[str] = mapped_column(Text, default="[]")
    adopted_asset_id: Mapped[str | None] = mapped_column(String(40))


class FrameCandidate(Base):
    __tablename__ = "frame_candidate"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    card_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("frame_card.id", ondelete="CASCADE"), index=True
    )
    asset_id: Mapped[str] = mapped_column(String(40))
    job_id: Mapped[str | None] = mapped_column(String(40))
    snapshot_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[str] = mapped_column(String(40), default=utc_now)

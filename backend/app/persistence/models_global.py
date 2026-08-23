"""Application-level resources that are shared by all projects."""

from __future__ import annotations

from sqlalchemy import Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.persistence.models import utc_now


class GlobalBase(DeclarativeBase):
    """Metadata for the application registry, separate from project databases."""


class GlobalWorkflow(GlobalBase):
    __tablename__ = "workflow"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    capability: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    api_json: Mapped[str] = mapped_column(Text, nullable=False)
    bindings_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    nodes_json: Mapped[str | None] = mapped_column(Text)
    required_nodes_json: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    validation_json: Mapped[str | None] = mapped_column(Text)
    is_default: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utc_now)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utc_now)

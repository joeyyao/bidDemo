from datetime import datetime, timezone
from sqlalchemy import String, Text, DateTime, ForeignKey, Integer, LargeBinary
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .database import Base

def now(): return datetime.now(timezone.utc)

class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

class Contract(Base):
    __tablename__ = "contracts"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    contract_number: Mapped[str] = mapped_column(String(120), default="")
    requirements: Mapped[str] = mapped_column(Text)
    tender_filename: Mapped[str] = mapped_column(String(255), default="")
    tender_text: Mapped[str] = mapped_column(Text, default="")
    tender_compact_text: Mapped[str] = mapped_column(Text, default="")
    tender_html: Mapped[str] = mapped_column(Text, default="")
    tender_file_data: Mapped[bytes] = mapped_column(LargeBinary, default=b"")
    tender_layout_filename: Mapped[str] = mapped_column(String(255), default="")
    tender_layout_data: Mapped[bytes] = mapped_column(LargeBinary, default=b"", deferred=True)
    reference_filename: Mapped[str] = mapped_column(String(255), default="")
    reference_text: Mapped[str] = mapped_column(Text, default="")
    reference_compact_text: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(30), default="planning")
    final_text: Mapped[str] = mapped_column(Text, default="")
    final_check: Mapped[str] = mapped_column(Text, default="")
    final_document_json: Mapped[str] = mapped_column(Text, default="")
    document_header: Mapped[str] = mapped_column(Text, default="")
    document_footer: Mapped[str] = mapped_column(Text, default="")
    page_number_format: Mapped[str] = mapped_column(String(120), default="第 {page} 页")
    generation_id: Mapped[str] = mapped_column(String(36), default="")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
    node_models: Mapped[str] = mapped_column(Text, default='{"planner":"qwen3.8-flash","chapter_default":"qwen3.8-flash"}')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    chapters: Mapped[list["Chapter"]] = relationship(back_populates="contract", cascade="all, delete-orphan")
    versions: Mapped[list["Version"]] = relationship(back_populates="contract", cascade="all, delete-orphan")
    events: Mapped[list["WorkflowEvent"]] = relationship(back_populates="contract", cascade="all, delete-orphan")

class Chapter(Base):
    __tablename__ = "chapters"
    id: Mapped[int] = mapped_column(primary_key=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("contracts.id"), index=True)
    order: Mapped[int] = mapped_column(Integer, default=0)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    knowledge_points: Mapped[str] = mapped_column(Text, default="")
    source_text: Mapped[str] = mapped_column(Text, default="")
    reference_text: Mapped[str] = mapped_column(Text, default="")
    plan: Mapped[str] = mapped_column(Text, default="")
    check_criteria: Mapped[str] = mapped_column(Text, default="")
    prompt: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str] = mapped_column(String(80), default="qwen3.8-flash")
    content: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(30), default="pending")
    self_check: Mapped[str] = mapped_column(Text, default="")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
    contract: Mapped[Contract] = relationship(back_populates="chapters")
    versions: Mapped[list["Version"]] = relationship(back_populates="chapter", cascade="all, delete-orphan")

class Version(Base):
    __tablename__ = "versions"
    id: Mapped[int] = mapped_column(primary_key=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("contracts.id"), index=True)
    chapter_id: Mapped[int | None] = mapped_column(ForeignKey("chapters.id"), nullable=True)
    version_no: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    contract: Mapped[Contract] = relationship(back_populates="versions")
    chapter: Mapped[Chapter | None] = relationship(back_populates="versions")

class WorkflowEvent(Base):
    __tablename__ = "workflow_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("contracts.id"), index=True)
    node_key: Mapped[str] = mapped_column(String(80))
    node_name: Mapped[str] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(30), default="completed")
    model: Mapped[str] = mapped_column(String(80), default="")
    message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    contract: Mapped[Contract] = relationship(back_populates="events")

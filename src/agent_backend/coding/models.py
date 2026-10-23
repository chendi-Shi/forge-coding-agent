from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now():
    return datetime.now(UTC)


def new_id():
    return str(uuid4())


class CodingBase(DeclarativeBase):
    pass


class Project(CodingBase):
    __tablename__ = "coding_projects"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner: Mapped[str] = mapped_column(Text, index=True)
    name: Mapped[str] = mapped_column(Text)
    workspace_id: Mapped[str] = mapped_column(String(36))
    source: Mapped[str] = mapped_column(Text, default="upload")
    test_command: Mapped[str] = mapped_column(Text, default="")
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Task(CodingBase):
    __tablename__ = "coding_tasks"
    __table_args__ = (UniqueConstraint("owner", "client_request_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner: Mapped[str] = mapped_column(Text, index=True)
    project_id: Mapped[str] = mapped_column(String(36), index=True)
    project_name: Mapped[str] = mapped_column(Text)
    project_version: Mapped[int] = mapped_column(Integer)
    source_workspace_id: Mapped[str] = mapped_column(String(36))
    parent_task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    seed_workspace_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    workspace_ready: Mapped[bool] = mapped_column(Boolean, default=False)
    prompt: Mapped[str] = mapped_column(Text)
    engine: Mapped[str] = mapped_column(String(32), default="codex", index=True)
    runtime_state: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    checkpoint_revision: Mapped[int] = mapped_column(Integer, default=0)
    model: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    test_command: Mapped[str] = mapped_column(Text, default="")
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=900)
    client_request_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    executor_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    lease_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    session_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    usage: Mapped[dict] = mapped_column(JSONB, default=dict)
    baseline_test: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    verification: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    diff: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Event(CodingBase):
    __tablename__ = "coding_events"
    __table_args__ = (UniqueConstraint("task_id", "sequence"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(36), index=True)
    sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    kind: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Executor(CodingBase):
    __tablename__ = "coding_executors"
    owner: Mapped[str] = mapped_column(Text, primary_key=True)
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    capacity: Mapped[int] = mapped_column(Integer, default=2)
    engine: Mapped[str] = mapped_column(String(32), default="codex")
    version: Mapped[str] = mapped_column(Text, default="")
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ToolOperation(CodingBase):
    __tablename__ = "coding_tool_operations"
    task_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    call_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="running")
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

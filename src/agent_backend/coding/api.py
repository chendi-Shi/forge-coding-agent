"""Owner-scoped coding projects, durable pull queue, events and patch review."""

import asyncio
import base64
import hashlib
import io
import json
import os
import re
import secrets
import zipfile
from datetime import timedelta
from typing import Literal
from urllib.parse import quote, urlsplit
from uuid import UUID, uuid4, uuid5

import httpx
from aegra_api.core.orm import get_session
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from agent_backend.coding.broker import broker, tool
from agent_backend.coding.models import Event, Executor, Project, Task, ToolOperation, now
from agent_backend.coding.sample import SAMPLE_FILES, SAMPLE_PROMPT, SAMPLE_TEST
from agent_backend.coding.verification import (
    AcceptanceSuite,
    AcceptanceSuiteCreate,
    acceptance_gate,
    archive_files,
    content_digest,
    create_suite,
    suite_public,
    verify_suite,
)
from agent_backend.settings import identity_from_header

router = APIRouter(prefix="/api/coding", tags=["Coding Agent"])
ACTIVE = {"running", "verifying"}
TERMINAL = {"awaiting_review", "accepted", "failed", "cancelled", "rejected"}
MAX_ARCHIVE = 16 * 1024 * 1024


def identity(request: Request):
    owner = identity_from_header(request.headers.get("authorization", ""))
    if owner is None:
        raise HTTPException(401, "Valid bearer token required")
    return owner


def public(row):
    hidden = {"owner", "lease_hash", "lease_expires_at", "source_workspace_id", "seed_workspace_id", "runtime_state"}
    return {
        column.name: (value.isoformat() if hasattr(value, "isoformat") else value)
        for column in row.__table__.columns
        if column.name not in hidden
        for value in [getattr(row, column.name)]
    }


async def owned(session, model, identifier, owner, *, lock=False):
    statement = select(model).where(model.id == str(identifier), model.owner == owner)
    if lock:
        statement = statement.with_for_update()
    row = await session.scalar(statement)
    if row is None:
        raise HTTPException(404, "Resource not found")
    return row


def leased(task, token):
    if (
        task.status not in ACTIVE or not task.lease_hash or not token
        or not secrets.compare_digest(task.lease_hash, hashlib.sha256(token.encode()).hexdigest())
        or task.lease_expires_at is None or task.lease_expires_at < now()
    ):
        raise HTTPException(409, "Task lease is no longer valid")


async def event_lock(session, task_id):
    # Flush task changes first so all writers acquire Task before the event
    # lock. Serialize event ID allocation through commit: SSE cursors cannot
    # skip an earlier event that commits after a later event.
    await session.flush()
    await session.execute(text("SELECT pg_advisory_xact_lock(82514, hashtext(:task_id))"), {"task_id": task_id})


async def record(session, task_id, kind, payload):
    await event_lock(session, task_id)
    session.add(Event(task_id=task_id, kind=kind, payload=payload))


async def reap(session, owner):
    expired = (await session.scalars(select(Task).where(
        Task.owner == owner, Task.status.in_(ACTIVE), Task.lease_expires_at < now(),
    ).with_for_update(skip_locked=True))).all()
    for task in expired:
        task.status = "cancelled" if task.cancel_requested else "failed"
        task.error = "Executor heartbeat expired. Work was not automatically replayed; inspect the events before retrying."
        task.finished_at = now()
        await record(session, task.id, "executor.lost", {"message": task.error})
    if expired:
        await session.commit()


async def verify_workspace(workspace_id, command):
    # Tests may write snapshots, caches or even source. Never let their side
    # effects change the immutable artifact that a human will review/accept.
    verification_id = str(uuid4())
    await broker("POST", f"/workspaces/{workspace_id}/fork", json={"workspace_id": verification_id})
    try:
        return await tool(verification_id, "run_command", {"command": command, "timeout_seconds": 120})
    finally:
        try:
            await broker("DELETE", f"/workspaces/{verification_id}")
        except HTTPException:
            # A cleanup outage must not mask the actual verification result.
            import logging
            logging.getLogger(__name__).warning("Verification workspace cleanup failed: %s", verification_id)


async def verify_task_workspace(session, task, workspace_id):
    """Pinned acceptance is mandatory even if a task overrides its dev command."""
    suite_id = getattr(task, "acceptance_suite_id", None)
    if not suite_id:
        return await verify_workspace(workspace_id, task.test_command)
    suite = await owned(session, AcceptanceSuite, suite_id, task.owner)
    if suite.project_id != task.project_id or suite.digest != task.acceptance_suite_digest:
        raise HTTPException(409, "Pinned acceptance suite does not match this task")
    result = await verify_suite(workspace_id, suite)
    if task.test_command and result.get("exit_code") == 0:
        development = await verify_workspace(workspace_id, task.test_command)
        result["development_tests"] = development
        if development.get("exit_code") != 0:
            result["exit_code"] = development.get("exit_code") if development.get("exit_code") is not None else 1
        result["stdout"] = (result.get("stdout", "") + "\nProject tests:\n" + development.get("stdout", ""))[-64000:]
        result["stderr"] = (result.get("stderr", "") + "\n" + development.get("stderr", ""))[-16000:]
    return result


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    files: dict[str, str] | None = None
    archive_base64: str | None = Field(default=None, max_length=24_000_000)
    repository_url: str | None = Field(default=None, max_length=500)
    ref: str = Field(default="HEAD", max_length=150)
    test_command: str = Field(default="", max_length=2000)


class TaskCreate(BaseModel):
    project_id: UUID
    prompt: str = Field(min_length=5, max_length=32000)
    model: str | None = Field(default=None, max_length=120)
    timeout_seconds: int = Field(default=900, ge=60, le=3600)
    test_command: str | None = Field(default=None, max_length=2000)
    client_request_id: UUID | None = None
    parent_task_id: UUID | None = None
    engine: Literal["forge", "codex"] = "forge"


class Registration(BaseModel):
    executor_id: str = Field(min_length=1, max_length=150, pattern=r"^[A-Za-z0-9_.:-]+$")
    capacity: int = Field(default=2, ge=1, le=100)
    version: str = Field(default="", max_length=120)
    engine: Literal["forge", "codex"] = "codex"


class Heartbeat(BaseModel):
    executor_id: str
    task_id: UUID
    lease_token: str


class EventBatch(BaseModel):
    lease_token: str
    events: list[dict] = Field(max_length=100)


class Completion(BaseModel):
    lease_token: str
    exit_code: int
    summary: str = Field(default="", max_length=100000)
    session_id: str | None = Field(default=None, max_length=200)
    usage: dict = Field(default_factory=dict)
    archive_base64: str | None = Field(default=None, max_length=24_000_000)
    error: str | None = Field(default=None, max_length=10000)


async def github_archive(url, ref):
    parsed = urlsplit(url)
    parts = parsed.path.strip("/").removesuffix(".git").split("/")
    if (
        parsed.scheme != "https" or parsed.netloc != "github.com" or parsed.query or parsed.fragment
        or len(parts) != 2 or any(not re.fullmatch(r"[A-Za-z0-9_.-]+", p) for p in parts)
    ):
        raise HTTPException(422, "Use a public https://github.com/owner/repository URL; supply branch in ref")
    address = f"https://codeload.github.com/{parts[0]}/{parts[1]}/zip/{quote(ref, safe='')}"
    data = bytearray()
    try:
        async with httpx.AsyncClient(timeout=90, trust_env=False) as client, client.stream("GET", address) as response:
            if response.status_code != 200:
                raise HTTPException(422, "GitHub repository/ref is not publicly accessible")
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > MAX_ARCHIVE:
                    raise HTTPException(413, "Repository archive exceeds 16 MiB")
        # GitHub adds one archive directory; remove only that wrapper.
        output = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target:
            total = 0
            for item in source.infolist():
                if item.is_dir():
                    continue
                name = item.filename.partition("/")[2]
                total += item.file_size
                if total > 32 * 1024 * 1024 or len(source.infolist()) > 3000:
                    raise HTTPException(413, "Expanded repository exceeds limits")
                # Keep external attributes: broker must reject symlink entries.
                copied = zipfile.ZipInfo(name)
                copied.external_attr = item.external_attr
                copied.compress_type = zipfile.ZIP_DEFLATED
                target.writestr(copied, source.read(item))
        return base64.b64encode(output.getvalue()).decode()
    except (httpx.HTTPError, zipfile.BadZipFile):
        raise HTTPException(422, "Unable to download GitHub source archive") from None


@router.get("/status")
async def status(owner=Depends(identity), session: AsyncSession = Depends(get_session)):
    await reap(session, owner)
    executors = (await session.scalars(select(Executor).where(
        Executor.owner == owner, Executor.last_seen > now() - timedelta(seconds=40),
    ))).all()
    counts = dict((await session.execute(select(Task.status, func.count()).where(Task.owner == owner).group_by(Task.status))).all())
    return {
        "engine": "forge", "executor_online": bool(executors),
        "executors": [public(item) for item in executors], "counts": counts,
        "capacity": {"coding_workers": sum(item.capacity for item in executors), "queue_limit": 100,
                     "sandbox_commands": int(os.environ.get("SANDBOX_MAX_CONCURRENT", "2"))},
        "execution_note": "Forge owns the reasoning loop, retrieval, budgets and tool execution. Forge tools and verification run in Docker; legacy Codex tasks run in local copies.",
    }


@router.get("/projects")
async def projects(owner=Depends(identity), session: AsyncSession = Depends(get_session)):
    rows = await session.scalars(select(Project).where(Project.owner == owner).order_by(Project.created_at.desc()))
    return {"projects": [public(row) for row in rows]}


@router.post("/projects", status_code=201)
async def create_project(body: ProjectCreate, owner=Depends(identity), session: AsyncSession = Depends(get_session)):
    if sum(value is not None for value in [body.files, body.archive_base64, body.repository_url]) != 1:
        raise HTTPException(422, "Supply exactly one of files, archive_base64 or repository_url")
    identifier = str(uuid4())
    payload = {"workspace_id": identifier}
    if body.repository_url:
        payload["archive_base64"] = await github_archive(body.repository_url, body.ref)
    elif body.archive_base64 is not None:
        payload["archive_base64"] = body.archive_base64
    else:
        if not body.files or len(body.files) > 1000 or sum(len(v.encode()) for v in body.files.values()) > MAX_ARCHIVE:
            raise HTTPException(413, "Project files exceed import limits")
        payload["files"] = [{"path": path, "content": content} for path, content in body.files.items()]
    await broker("POST", "/workspaces", json=payload)
    project = Project(id=identifier, owner=owner, name=body.name, workspace_id=identifier,
                      source=body.repository_url or "upload", test_command=body.test_command)
    session.add(project)
    await session.commit()
    return public(project)


@router.post("/projects/sample", status_code=201)
async def sample(owner=Depends(identity), session: AsyncSession = Depends(get_session)):
    project = await create_project(ProjectCreate(name="订单金额计算 · Python", files=SAMPLE_FILES, test_command=SAMPLE_TEST), owner, session)
    row = await owned(session, Project, project["id"], owner, lock=True)
    suite = await create_suite(session, row, AcceptanceSuiteCreate(
        name="订单金额独立回归", files={"test_order_acceptance.py": SAMPLE_FILES["tests/test_orders.py"]},
        test_paths=["test_order_acceptance.py"], protected_paths=["tests/test_orders.py"], minimum_tests=5,
    ))
    row.acceptance_suite_id = suite.id
    await session.commit()
    project = public(row)
    return {**project, "suggested_prompt": SAMPLE_PROMPT}


@router.get("/projects/{project_id}/files")
async def project_files(project_id: UUID, owner=Depends(identity), session: AsyncSession = Depends(get_session)):
    project = await owned(session, Project, project_id, owner)
    return await tool(project.workspace_id, "list_files", {})


@router.get("/projects/{project_id}/file")
async def project_file(project_id: UUID, path: str, owner=Depends(identity), session: AsyncSession = Depends(get_session)):
    project = await owned(session, Project, project_id, owner)
    return await tool(project.workspace_id, "read_file", {"path": path})


@router.get("/projects/{project_id}/acceptance-suites")
async def acceptance_suites(project_id: UUID, owner=Depends(identity), session: AsyncSession = Depends(get_session)):
    project = await owned(session, Project, project_id, owner)
    rows = await session.scalars(select(AcceptanceSuite).where(
        AcceptanceSuite.project_id == project.id, AcceptanceSuite.owner == owner,
    ).order_by(AcceptanceSuite.version.desc()))
    return {"active_suite_id": project.acceptance_suite_id, "suites": [suite_public(row) for row in rows]}


@router.post("/projects/{project_id}/acceptance-suites", status_code=201)
async def register_acceptance_suite(project_id: UUID, body: AcceptanceSuiteCreate, owner=Depends(identity), session: AsyncSession = Depends(get_session)):
    project = await owned(session, Project, project_id, owner, lock=True)
    suite = await create_suite(session, project, body)
    project.acceptance_suite_id = suite.id
    await session.commit()
    return suite_public(suite)


@router.post("/tasks", status_code=201)
async def create_task(body: TaskCreate, owner=Depends(identity), session: AsyncSession = Depends(get_session)):
    # Serializes admission per identity across API replicas.
    await session.execute(text("SELECT pg_advisory_xact_lock(82513, hashtext(:owner))"), {"owner": owner})
    if body.client_request_id:
        existing = await session.scalar(select(Task).where(Task.owner == owner, Task.client_request_id == str(body.client_request_id)))
        if existing:
            return public(existing)
    project = await owned(session, Project, body.project_id, owner, lock=True)
    pinned_suite = await owned(session, AcceptanceSuite, project.acceptance_suite_id, owner) if project.acceptance_suite_id else None
    parent = None
    if body.parent_task_id:
        parent = await owned(session, Task, body.parent_task_id, owner)
        if parent.project_id != project.id or parent.status not in TERMINAL or not parent.workspace_ready:
            raise HTTPException(409, "Follow-up requires a finished task from the same project")
        if parent.project_version != project.version:
            raise HTTPException(409, "Project version changed. Start a task on the latest project instead.")
    active = await session.scalar(select(func.count()).select_from(Task).where(Task.owner == owner, Task.status.in_(["queued", *ACTIVE])))
    if active >= 100:
        raise HTTPException(429, "100 unfinished tasks already queued for this identity")
    task = Task(
        id=str(uuid4()), owner=owner, project_id=project.id, project_name=project.name,
        project_version=project.version, source_workspace_id=project.workspace_id, prompt=body.prompt,
        model=body.model, timeout_seconds=body.timeout_seconds,
        test_command=project.test_command if body.test_command is None else body.test_command,
        client_request_id=str(body.client_request_id) if body.client_request_id else None,
        parent_task_id=parent.id if parent else None, seed_workspace_id=parent.id if parent else None,
        engine=body.engine,
        acceptance_suite_id=pinned_suite.id if pinned_suite else None,
        acceptance_suite_digest=pinned_suite.digest if pinned_suite else None,
    )
    session.add(task)
    await record(session, task.id, "task.queued", {"project_version": project.version, "engine": task.engine,
                                               "acceptance_suite_id": task.acceptance_suite_id, "acceptance_suite_digest": task.acceptance_suite_digest})
    await session.commit()
    return public(task)


@router.get("/tasks")
async def tasks(project_id: UUID | None = None, owner=Depends(identity), session: AsyncSession = Depends(get_session)):
    await reap(session, owner)
    statement = select(Task).where(Task.owner == owner).order_by(Task.created_at.desc()).limit(200)
    if project_id:
        statement = statement.where(Task.project_id == str(project_id))
    return {"tasks": [public(row) for row in await session.scalars(statement)]}


@router.get("/tasks/{task_id}")
async def task_detail(task_id: UUID, owner=Depends(identity), session: AsyncSession = Depends(get_session)):
    await reap(session, owner)
    return public(await owned(session, Task, task_id, owner))


@router.post("/tasks/{task_id}/cancel")
async def cancel(task_id: UUID, owner=Depends(identity), session: AsyncSession = Depends(get_session)):
    task = await owned(session, Task, task_id, owner, lock=True)
    if task.status in TERMINAL:
        return public(task)
    task.cancel_requested = True
    if task.status == "queued":
        task.status, task.finished_at = "cancelled", now()
    await record(session, task.id, "task.cancel_requested", {})
    await session.commit()
    return public(task)


@router.post("/tasks/{task_id}/accept")
async def accept(task_id: UUID, owner=Depends(identity), session: AsyncSession = Depends(get_session)):
    task = await owned(session, Task, task_id, owner, lock=True)
    if task.status == "accepted":
        return public(task)
    if task.status != "awaiting_review":
        raise HTTPException(409, "Only a finished task awaiting review can be accepted")
    project = await owned(session, Project, task.project_id, owner, lock=True)
    if project.version != task.project_version:
        raise HTTPException(409, "Project has changed since this task started. Download its patch or create a new task on the latest version.")
    if task.verification and task.verification.get("exit_code") != 0:
        raise HTTPException(409, "Independent tests failed. Fix the task before accepting it.")
    if not task.acceptance_suite_id or not project.acceptance_suite_id:
        raise HTTPException(409, "ACCEPTANCE_SUITE_REQUIRED: configure a suite and create a new task before accepting changes")
    suite = await owned(session, AcceptanceSuite, task.acceptance_suite_id, owner)
    if suite.project_id != project.id or suite.digest != task.acceptance_suite_digest:
        raise HTTPException(409, "Acceptance policy does not match the task")
    candidate = await broker("GET", f"/workspaces/{task.id}/archive", raw=True)
    gate = acceptance_gate(
        task_status=task.status, task_suite_id=task.acceptance_suite_id,
        active_suite_id=project.acceptance_suite_id, suite_digest=task.acceptance_suite_digest,
        candidate_digest=content_digest(archive_files(candidate)), verification=task.verification,
        minimum_tests=suite.policy["minimum_tests"],
    )
    if not gate["allowed"]:
        raise HTTPException(409, "Acceptance refused: " + gate["code"])
    # Immutable snapshots: accepting advances a DB pointer, never overwrites
    # a host checkout or a concurrently executing task's files.
    project.workspace_id, project.version = task.id, project.version + 1
    task.status = "accepted"
    await record(session, task.id, "task.accepted", {"project_version": project.version})
    await session.commit()
    return public(task)


@router.post("/tasks/{task_id}/reject")
async def reject(task_id: UUID, owner=Depends(identity), session: AsyncSession = Depends(get_session)):
    task = await owned(session, Task, task_id, owner, lock=True)
    if task.status != "awaiting_review":
        raise HTTPException(409, "Task is not awaiting review")
    task.status = "rejected"
    await record(session, task.id, "task.rejected", {})
    await session.commit()
    return public(task)


@router.get("/tasks/{task_id}/patch")
async def patch(task_id: UUID, owner=Depends(identity), session: AsyncSession = Depends(get_session)):
    task = await owned(session, Task, task_id, owner)
    return Response(task.diff, media_type="text/x-diff", headers={"Content-Disposition": f'attachment; filename="{task.id}.patch"'})


@router.get("/tasks/{task_id}/archive")
async def archive(task_id: UUID, owner=Depends(identity), session: AsyncSession = Depends(get_session)):
    task = await owned(session, Task, task_id, owner)
    if not task.workspace_ready:
        raise HTTPException(409, "Task workspace is not ready")
    data = await broker("GET", f"/workspaces/{task.id}/archive", raw=True)
    return Response(data, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{task.id}.zip"'})


async def event_rows(session, task_id, after):
    rows = await session.scalars(select(Event).where(Event.task_id == task_id, Event.id > after).order_by(Event.id).limit(200))
    return [public(row) for row in rows]


@router.get("/tasks/{task_id}/events")
async def events(task_id: UUID, after: int = Query(0, ge=0), owner=Depends(identity), session: AsyncSession = Depends(get_session)):
    await owned(session, Task, task_id, owner)
    return {"events": await event_rows(session, str(task_id), after)}


@router.get("/tasks/{task_id}/stream")
async def event_stream(task_id: UUID, request: Request, after: int = Query(0, ge=0), owner=Depends(identity), session: AsyncSession = Depends(get_session)):
    await owned(session, Task, task_id, owner)
    await session.close()

    async def stream():
        from aegra_api.core.orm import get_session_maker
        cursor = after
        while not await request.is_disconnected():
            async with get_session_maker()() as current:
                state = await current.get(Task, str(task_id))
                terminal = state is None or state.status in TERMINAL
                # Read terminal state first. A terminal commit racing these
                # reads must not make us end before its final events arrive.
                rows = await event_rows(current, str(task_id), cursor)
            for row in rows:
                cursor = row["id"]
                yield f"id: {cursor}\nevent: coding\ndata: {json.dumps(row, ensure_ascii=False)}\n\n"
            if terminal and len(rows) < 200:
                yield "event: end\ndata: {}\n\n"
                return
            yield ": keepalive\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def register_worker(session, body, owner):
    await session.execute(insert(Executor).values(
        owner=owner, id=body.executor_id, capacity=body.capacity, version=body.version, engine=body.engine, last_seen=now(),
    ).on_conflict_do_update(index_elements=[Executor.owner, Executor.id], set_={
        "capacity": body.capacity, "version": body.version, "engine": body.engine, "last_seen": now(),
    }))


@router.post("/executors/register")
async def register(body: Registration, owner=Depends(identity), session: AsyncSession = Depends(get_session)):
    await register_worker(session, body, owner)
    await session.commit()
    return {"ok": True}


@router.post("/executors/claim")
async def claim(body: Registration, owner=Depends(identity), session: AsyncSession = Depends(get_session)):
    await reap(session, owner)
    await register_worker(session, body, owner)
    count = await session.scalar(select(func.count()).select_from(Task).where(Task.owner == owner, Task.executor_id == body.executor_id, Task.status.in_(ACTIVE)))
    if count >= body.capacity:
        await session.commit()
        return {"task": None}
    task = await session.scalar(select(Task).where(Task.owner == owner, Task.status == "queued", Task.engine == body.engine).order_by(Task.created_at).with_for_update(skip_locked=True).limit(1))
    if not task:
        await session.commit()
        return {"task": None}
    token = secrets.token_urlsafe(32)
    task.status, task.executor_id = "running", body.executor_id
    task.started_at = task.started_at or now()
    task.lease_hash = hashlib.sha256(token.encode()).hexdigest()
    task.lease_expires_at = now() + timedelta(seconds=120)
    await record(session, task.id, "task.started", {"executor_id": body.executor_id, "engine": task.engine, "sandbox": "Docker tools" if task.engine == "forge" else "codex workspace-write"})
    await session.commit()
    return {"task": {**public(task), "lease_token": token}}


@router.post("/executors/heartbeat")
async def heartbeat(body: Heartbeat, owner=Depends(identity), session: AsyncSession = Depends(get_session)):
    task = await owned(session, Task, body.task_id, owner, lock=True)
    leased(task, body.lease_token)
    if task.executor_id != body.executor_id:
        raise HTTPException(409, "Executor does not own this lease")
    task.lease_expires_at = max(task.lease_expires_at, now() + timedelta(seconds=120))
    await session.execute(update(Executor).where(Executor.owner == owner, Executor.id == body.executor_id).values(last_seen=now()))
    await session.commit()
    return {"cancel_requested": task.cancel_requested}


@router.get("/tasks/{task_id}/source")
async def source(task_id: UUID, x_lease_token: str = Header(default=""), owner=Depends(identity), session: AsyncSession = Depends(get_session)):
    def check_source():
        leased(task, x_lease_token)
        if task.status != "running" or task.cancel_requested:
            raise HTTPException(409, "Source requires a running task without cancellation")
        if task.workspace_ready and (task.test_command or getattr(task, "acceptance_suite_id", None)) and task.baseline_test is None:
            # Older deployments could publish readiness before recording the
            # baseline. The task or its failed parent may since have changed.
            raise HTTPException(409, "Initial baseline unavailable for this existing workspace; create a follow-up instead of reconstructing it from modified files")

    task = await owned(session, Task, task_id, owner, lock=True)
    check_source()
    if not task.workspace_ready:
        task.lease_expires_at = max(task.lease_expires_at, now() + timedelta(seconds=360))
    await session.commit()  # Release the row so heartbeat/cancel remain available.
    if not task.workspace_ready:
        # Only source requests use this transaction-level lock. No commits or
        # Task writes occur during external I/O, so heartbeats do not wait for
        # a test command while concurrent source retries remain serialized.
        await session.execute(text("SELECT pg_advisory_xact_lock(82515, hashtext(:task_id))"), {"task_id": task.id})
        await session.refresh(task)
        check_source()
        if not task.workspace_ready:
            # A failed parent can resume and mutate its workspace. Freeze its
            # initial content once, using an idempotent, task-derived identity.
            initial_id = str(uuid5(UUID(task.id), "coding-initial-workspace-v1"))
            try:
                initial = await broker("GET", f"/workspaces/{initial_id}/archive", raw=True)
            except HTTPException as exc:
                if exc.status_code != 404:
                    raise
                initial_source = task.seed_workspace_id or task.source_workspace_id
                await broker("POST", f"/workspaces/{initial_source}/fork", json={"workspace_id": initial_id})
                initial = await broker("GET", f"/workspaces/{initial_id}/archive", raw=True)
            # An archive can exist on a partially initialized volume. Require
            # its committed Git baseline before treating these bytes as initial.
            await broker("POST", f"/workspaces/{initial_id}/tools", json={"name": "get_diff", "arguments": {"max_output": 1}})
            await broker("POST", f"/workspaces/{task.source_workspace_id}/fork", json={"workspace_id": task.id})
            await broker("POST", f"/workspaces/{task.id}/restore", json={"archive_base64": base64.b64encode(initial).decode()})
            baseline = await verify_task_workspace(session, task, initial_id) if task.test_command or getattr(task, "acceptance_suite_id", None) else None
            # Never publish a workspace before its baseline result is durable,
            # or let a cancelled/expired/replaced lease publish late results.
            with session.no_autoflush:
                await session.refresh(task, with_for_update=True)
            check_source()
            task.baseline_test, task.workspace_ready = baseline, True
            if baseline is not None:
                await record(session, task.id, "test.baseline", baseline)
        await session.commit()  # Also releases the source advisory lock.
    data = await broker("GET", f"/workspaces/{task.id}/archive", raw=True)
    return Response(data, media_type="application/zip")


@router.post("/tasks/{task_id}/events")
async def append_events(task_id: UUID, body: EventBatch, owner=Depends(identity), session: AsyncSession = Depends(get_session)):
    task = await owned(session, Task, task_id, owner)
    leased(task, body.lease_token)
    if len(json.dumps(body.events)) > 512000:
        raise HTTPException(413, "Event batch too large")
    await event_lock(session, task.id)
    for envelope in body.events:
        sequence = envelope.get("sequence")
        event = envelope.get("event", envelope)
        if not isinstance(sequence, int) or sequence < 0 or not isinstance(event, dict):
            raise HTTPException(422, "Expected {sequence:int,event:object} events")
        # Bound persistable output; detailed local logs remain with the worker.
        serialized = json.dumps(event, ensure_ascii=False)
        if len(serialized) > 65536:
            event = {"type": event.get("type", "event"), "truncated": True, "preview": serialized[:65000]}
        await session.execute(insert(Event).values(
            task_id=task.id, sequence=sequence, kind=str(event.get("type", "agent.event"))[:120], payload=event,
        ).on_conflict_do_nothing(index_elements=[Event.task_id, Event.sequence]))
    await session.commit()
    return {"ok": True}


@router.post("/tasks/{task_id}/complete")
async def complete(task_id: UUID, body: Completion, owner=Depends(identity), session: AsyncSession = Depends(get_session)):
    task = await owned(session, Task, task_id, owner, lock=True)
    # Allows a worker to retry completion if the successful response was lost.
    if task.status in TERMINAL and task.lease_hash and secrets.compare_digest(task.lease_hash, hashlib.sha256(body.lease_token.encode()).hexdigest()):
        return public(task)
    leased(task, body.lease_token)
    if task.status == "verifying":
        raise HTTPException(409, "Completion is already being verified")
    if body.exit_code == 0 and task.engine == "forge" and await session.scalar(select(ToolOperation).where(ToolOperation.task_id == task.id, ToolOperation.status == "running").limit(1)):
        raise HTTPException(409, "A tool operation is still running or has an uncertain outcome")
    task.status, task.summary, task.session_id, task.usage = "verifying", body.summary, body.session_id, body.usage
    task.lease_expires_at = now() + timedelta(seconds=360)
    await session.commit()
    changes = verification = failure = None
    try:
        if task.workspace_ready and (body.archive_base64 or task.engine == "forge"):
            if body.archive_base64 and task.engine != "forge":
                await broker("POST", f"/workspaces/{task.id}/restore", json={"archive_base64": body.archive_base64})
            changes = await broker("GET", f"/workspaces/{task.id}/diff")
        elif body.exit_code == 0:
            raise HTTPException(422, "Successful completion requires a workspace archive")
        if body.exit_code == 0 and (task.test_command or getattr(task, "acceptance_suite_id", None)):
            await record(session, task.id, "test.started", {"command": task.test_command, "environment": "isolated Docker; network disabled",
                                                          "acceptance_suite_id": getattr(task, "acceptance_suite_id", None)})
            await session.commit()
            verification = await verify_task_workspace(session, task, task.id)
    except HTTPException as exc:
        failure = str(exc.detail)
    # Keep external results out of the identity map until the control fields
    # have been reloaded under a row lock. This covers error exits as well as
    # success, and prevents old completion requests overwriting a new lease.
    with session.no_autoflush:
        await session.refresh(task, with_for_update=True)
    if task.status != "verifying":
        await session.commit()
        return public(task)
    leased(task, body.lease_token)
    if changes is not None:
        task.diff = changes.get("diff", "")
        if changes.get("truncated"):
            await record(session, task.id, "artifact.truncated", {"message": "Diff exceeded preview limit. Download the workspace archive."})
    if verification is not None:
        task.verification = verification
        await record(session, task.id, "test.completed", verification)
    task.status = "cancelled" if task.cancel_requested else ("failed" if failure or body.exit_code else "awaiting_review")
    task.error = failure or body.error or (f"Coding engine exited with code {body.exit_code}" if body.exit_code else None)
    if failure:
        await record(session, task.id, "verification.error", {"message": task.error})
    task.finished_at = now()
    await record(session, task.id, "task.finished", {"status": task.status, "has_diff": bool(task.diff), "error": task.error})
    await session.commit()
    return public(task)

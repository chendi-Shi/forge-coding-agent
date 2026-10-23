"""Lease-fenced tools and compare-and-swap checkpoints for the Forge runtime."""

import json
import time
from datetime import timedelta
from uuid import UUID

from aegra_api.core.orm import get_session
from fastapi import APIRouter, Depends, Header, HTTPException
from jsonschema import ValidationError
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from agent_backend.coding.api import ACTIVE, identity, leased, owned, public, record, verify_workspace
from agent_backend.coding.broker import broker, tool
from agent_backend.coding.models import Event, Project, Task, ToolOperation, now
from agent_backend.forge.protocol import digest, validate_tool

router = APIRouter(prefix="/api/coding/tasks", tags=["Forge runtime"])


class Checkpoint(BaseModel):
    lease_token: str
    expected_revision: int = Field(ge=0)
    state: dict


class ToolRequest(BaseModel):
    lease_token: str
    call_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.:-]+$")
    name: str
    arguments: dict = Field(default_factory=dict)


def running(task, token):
    leased(task, token)
    if task.engine != "forge" or task.status != "running" or task.cancel_requested:
        raise HTTPException(409, "Forge task is not running or cancellation was requested")
    if not task.workspace_ready:
        raise HTTPException(409, "Fetch task source before invoking the runtime")


@router.get("/{task_id}/checkpoint")
async def get_checkpoint(task_id: UUID, x_lease_token: str = Header(""), owner=Depends(identity), session: AsyncSession = Depends(get_session)):
    task = await owned(session, Task, task_id, owner)
    running(task, x_lease_token)
    sequence = await session.scalar(select(func.max(Event.sequence)).where(Event.task_id == task.id))
    return {"revision": task.checkpoint_revision, "state": task.runtime_state, "event_sequence": sequence or 0}


@router.put("/{task_id}/checkpoint")
async def save_checkpoint(task_id: UUID, body: Checkpoint, owner=Depends(identity), session: AsyncSession = Depends(get_session)):
    task = await owned(session, Task, task_id, owner, lock=True)
    running(task, body.lease_token)
    if len(json.dumps(body.state, ensure_ascii=False).encode()) > 1_000_000:
        raise HTTPException(413, "Checkpoint exceeds 1 MB; compact observations first")
    if task.checkpoint_revision != body.expected_revision:
        raise HTTPException(409, "Checkpoint revision changed")
    task.runtime_state, task.checkpoint_revision = body.state, task.checkpoint_revision + 1
    await session.commit()
    return {"revision": task.checkpoint_revision}


@router.post("/{task_id}/resume")
async def resume(task_id: UUID, owner=Depends(identity), session: AsyncSession = Depends(get_session)):
    await session.execute(text("SELECT pg_advisory_xact_lock(82513, hashtext(:owner))"), {"owner": owner})
    active = await session.scalar(select(func.count()).select_from(Task).where(Task.owner == owner, Task.status.in_(["queued", *ACTIVE])))
    if active >= 100:
        raise HTTPException(429, "100 unfinished tasks already queued for this identity")
    task = await owned(session, Task, task_id, owner, lock=True)
    if task.engine != "forge" or task.status != "failed" or not task.runtime_state or not task.workspace_ready:
        raise HTTPException(409, "Only failed Forge tasks with a checkpoint can resume")
    if task.runtime_state.get("deadline", float("inf")) <= time.time():
        raise HTTPException(409, "Task time budget expired; create a follow-up with a new budget")
    if task.error and "BUDGET_EXHAUSTED" in task.error:
        raise HTTPException(409, "Task budget exhausted; create a follow-up with a new budget")
    uncertain = await session.scalar(select(ToolOperation).where(ToolOperation.task_id == task.id, ToolOperation.status == "running").limit(1))
    if uncertain:
        raise HTTPException(409, "A tool has an uncertain outcome. Inspect its workspace and start a follow-up; it will not be replayed automatically.")
    project = await owned(session, Project, task.project_id, owner)
    if project.version != task.project_version:
        raise HTTPException(409, "Project version advanced; start a task on its latest version")
    task.status, task.error, task.finished_at = "queued", None, None
    task.cancel_requested, task.executor_id, task.lease_hash, task.lease_expires_at = False, None, None, None
    await record(session, task.id, "runtime.resumed", {"checkpoint_revision": task.checkpoint_revision})
    await session.commit()
    return public(task)


@router.get("/{task_id}/tool-operations")
async def operations(task_id: UUID, owner=Depends(identity), session: AsyncSession = Depends(get_session)):
    await owned(session, Task, task_id, owner)
    rows = await session.scalars(select(ToolOperation).where(ToolOperation.task_id == str(task_id)).order_by(ToolOperation.started_at))
    return {"operations": [public(row) for row in rows]}


@router.post("/{task_id}/tools")
async def execute_tool(task_id: UUID, body: ToolRequest, owner=Depends(identity), session: AsyncSession = Depends(get_session)):
    try:
        validate_tool(body.name, body.arguments)
    except (ValueError, ValidationError):
        raise HTTPException(422, "Tool arguments do not match their schema") from None
    if body.name in {"repository_search", "read_artifact"}:
        raise HTTPException(422, "This read-only tool runs against worker-local artifacts")
    task = await owned(session, Task, task_id, owner, lock=True)
    running(task, body.lease_token)
    fingerprint = digest({"name": body.name, "arguments": body.arguments})
    operation = await session.get(ToolOperation, (task.id, body.call_id))
    if operation:
        if operation.request_hash != fingerprint:
            raise HTTPException(409, "call_id reused with different arguments")
        if operation.status == "running":
            raise HTTPException(409, "Tool is running or its outcome is uncertain; automatic replay is forbidden")
        return {"call_id": body.call_id, "cached": True, "result": operation.result}
    if await session.scalar(select(ToolOperation).where(ToolOperation.task_id == task.id, ToolOperation.status == "running").limit(1)):
        raise HTTPException(409, "Another tool operation is in flight")
    operation = ToolOperation(task_id=task.id, call_id=body.call_id, name=body.name, request_hash=fingerprint)
    session.add(operation)
    task.lease_expires_at = max(task.lease_expires_at, now() + timedelta(seconds=360))
    await record(session, task.id, "tool.started", {"call_id": body.call_id, "tool": body.name, "arguments": body.arguments})
    await session.commit()  # Commit intent before external effects; never guess on crash.
    try:
        if body.name == "run_tests":
            if not task.test_command:
                result = {"error": "No verification command configured", "exit_code": None}
            else:
                result = await verify_workspace(task.id, task.test_command)
        elif body.name == "get_diff":
            result = await broker("GET", f"/workspaces/{task.id}/diff")
        elif body.name == "read_file":
            result = await tool(task.id, "read_file", {"path": body.arguments["path"]})
            lines = result.get("content", "").splitlines()
            first = body.arguments.get("start_line", 1)
            last = min(body.arguments.get("end_line", first + 199), first + 399)
            result = {"path": body.arguments["path"], "start_line": first, "end_line": min(last, len(lines)), "total_lines": len(lines), "content": "\n".join(f"{i}: {line}" for i, line in enumerate(lines[first - 1:last], first))[:40000]}
        else:
            arguments = dict(body.arguments)
            if body.name == "run_command":
                arguments.update(timeout_seconds=arguments.get("timeout_seconds", 60), max_output_bytes=32000)
            result = await tool(task.id, body.name, arguments)
        operation.status = "succeeded"
    except HTTPException as error:
        # A transport outage can occur after an effect. Preserve 'running' as
        # uncertain so a retry cannot silently repeat that command.
        if error.status_code >= 500:
            raise
        result, operation.status = {"error": str(error.detail), "status_code": error.status_code}, "failed"
    operation.result, operation.finished_at = result, now()
    await record(session, task.id, "tool.completed", {"call_id": body.call_id, "tool": body.name, "status": operation.status, "result": result})
    await session.commit()
    return {"call_id": body.call_id, "cached": False, "result": result}

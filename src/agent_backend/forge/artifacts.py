"""Task-scoped durable artifacts; database payloads precede checkpoint references.

The task row serializes uploads and quota checks across API replicas. No external
I/O runs while that row is locked. Content is immutable and addressed by kind and
UTF-8 bytes; task ownership remains mandatory even when the digest is known.
"""

import math
from uuid import UUID

from aegra_api.core.orm import get_session
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_backend.coding.api import identity, leased, owned
from agent_backend.coding.models import Artifact, Task
from agent_backend.forge.artifact_data import (
    ARTIFACT_ID,
    MAX_ARTIFACT_BYTES,
    MAX_TASK_ARTIFACT_BYTES,
    MAX_TASK_ARTIFACTS,
    PAGE_CHARACTERS,
    checkpoint_references,
    describe_artifact,
)

router = APIRouter(prefix="/api/coding/tasks", tags=["Forge artifacts"])


class ArtifactBody(BaseModel):
    kind: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    content: str = Field(max_length=MAX_ARTIFACT_BYTES)


def require_running(task, token):
    leased(task, token)
    if task.engine != "forge" or task.status != "running" or task.cancel_requested or not task.workspace_ready:
        raise HTTPException(409, "Artifact access requires a running Forge task with source ready")


def metadata(artifact):
    return {"uri": "artifact://" + artifact.artifact_id, "kind": artifact.kind, "sha256": artifact.sha256,
            "bytes": artifact.byte_length, "estimated_tokens": math.ceil(artifact.byte_length / 3)}


@router.put("/{task_id}/artifacts/{artifact_id}")
async def put_artifact(task_id: UUID, artifact_id: str, body: ArtifactBody, x_lease_token: str = Header(""),
                       owner=Depends(identity), session: AsyncSession = Depends(get_session)):
    if not ARTIFACT_ID.fullmatch(artifact_id):
        raise HTTPException(422, "Invalid artifact identity")
    try:
        expected = describe_artifact(body.content, body.kind)
    except ValueError as error:
        raise HTTPException(413 if "exceeds" in str(error) else 422, str(error)) from None
    if expected["uri"] != "artifact://" + artifact_id:
        raise HTTPException(422, "Artifact identity does not match its content")
    task = await owned(session, Task, task_id, owner, lock=True)
    require_running(task, x_lease_token)
    existing = await session.get(Artifact, (task.id, artifact_id))
    if existing:
        if metadata(existing) != expected or existing.content != body.content:
            raise HTTPException(409, "Artifact content is immutable")
        return {**metadata(existing), "created": False}
    count, total = (await session.execute(select(func.count(), func.coalesce(func.sum(Artifact.byte_length), 0))
                                         .where(Artifact.task_id == task.id))).one()
    if count >= MAX_TASK_ARTIFACTS or total + expected["bytes"] > MAX_TASK_ARTIFACT_BYTES:
        raise HTTPException(413, "Task artifact quota exceeded (256 artifacts / 16 MiB)")
    value = Artifact(task_id=task.id, artifact_id=artifact_id, kind=body.kind, sha256=expected["sha256"],
                     byte_length=expected["bytes"], content=body.content)
    session.add(value)
    await session.commit()
    return {**expected, "created": True}


@router.get("/{task_id}/artifacts/{artifact_id}")
async def get_artifact(task_id: UUID, artifact_id: str, offset: int = Query(0, ge=0),
                       limit: int = Query(PAGE_CHARACTERS, ge=1, le=PAGE_CHARACTERS), x_lease_token: str = Header(""),
                       owner=Depends(identity), session: AsyncSession = Depends(get_session)):
    if not ARTIFACT_ID.fullmatch(artifact_id):
        raise HTTPException(422, "Invalid artifact identity")
    task = await owned(session, Task, task_id, owner)
    require_running(task, x_lease_token)
    value = await session.get(Artifact, (task.id, artifact_id))
    if value is None:
        raise HTTPException(404, "Artifact is not available for this task")
    end = offset + limit
    return {**metadata(value), "offset": offset, "content": value.content[offset:end],
            "next_offset": end if end < len(value.content) else None, "offset_unit": "unicode_characters"}


async def validate_checkpoint_artifacts(session: AsyncSession, task: Task, state: dict):
    """Run inside the checkpoint's task row lock, before publishing its revision."""
    try:
        references = checkpoint_references(state)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    if not references:
        return
    rows = await session.execute(select(Artifact.artifact_id, Artifact.kind, Artifact.sha256, Artifact.byte_length)
                                 .where(Artifact.task_id == task.id, Artifact.artifact_id.in_(references)))
    found = {row.artifact_id: row for row in rows}
    for key, reference in references.items():
        value = found.get(key)
        if value is None:
            raise HTTPException(409, "Checkpoint references an artifact not yet persisted for this task")
        if reference != metadata(value):
            raise HTTPException(422, "Checkpoint artifact metadata does not match persisted content")

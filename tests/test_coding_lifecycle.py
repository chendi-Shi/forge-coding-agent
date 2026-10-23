"""Deterministic lifecycle faults; real PostgreSQL lock probes live in scripts/.

The session double models an identity map separately from committed state, so a
concurrent cancellation is not accidentally visible through a shared object.
"""

import base64
import copy
import hashlib
import importlib
from contextlib import nullcontext
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4, uuid5

import pytest
from fastapi import HTTPException

api = importlib.import_module("agent_backend.coding.api")
TOKEN = "test-current-lease"


def new_task(**overrides):
    values = dict(
        id=str(uuid4()), owner="lifecycle-test", status="running", engine="forge",
        source_workspace_id=str(uuid4()), seed_workspace_id=None, workspace_ready=False,
        test_command="python baseline.py", baseline_test=None, cancel_requested=False,
        lease_hash=hashlib.sha256(TOKEN.encode()).hexdigest(),
        lease_expires_at=api.now() + timedelta(seconds=120),
        diff="", verification=None, summary="", session_id=None, usage={}, error=None, finished_at=None,
    )
    return SimpleNamespace(**(values | overrides))


class Session:
    def __init__(self, task):
        self.task = task
        self.database = copy.deepcopy(vars(task))
        self.loaded = copy.deepcopy(vars(task))
        self.no_autoflush = nullcontext()
        self.refresh_hook = None
        self.final_locked = False

    async def scalar(self, _statement):
        return None  # No uncertain ToolOperation in these lifecycle fixtures.

    async def execute(self, *_args):
        return None

    async def commit(self):
        for name, value in vars(self.task).items():
            if value != self.loaded[name]:
                self.database[name] = copy.deepcopy(value)
        self.loaded = copy.deepcopy(vars(self.task))

    async def refresh(self, task, attribute_names=None, with_for_update=None):
        # For the finalization race, cancellation commits before a lock is
        # obtained, or immediately after an unlocked snapshot has been read.
        final = self.database["status"] == "verifying"
        if final and with_for_update and self.refresh_hook:
            self.refresh_hook(self)
        for name in attribute_names or self.database:
            setattr(task, name, copy.deepcopy(self.database[name]))
            self.loaded[name] = copy.deepcopy(self.database[name])
        self.final_locked = bool(with_for_update)
        if final and not with_for_update and self.refresh_hook:
            self.refresh_hook(self)

    async def rollback(self):
        await self.refresh(self.task)


class Workspaces:
    def __init__(self, task):
        self.files = {task.source_workspace_id: b"project before"}
        if task.seed_workspace_id:
            self.files[task.seed_workspace_id] = b"parent before"
        self.calls = []
        self.fork_origins = {}
        self.incomplete = set()

    async def __call__(self, method, path, **kwargs):
        self.calls.append((method, path))
        workspace, operation = path.split("/")[2:4]
        if operation == "fork":
            destination = kwargs["json"]["workspace_id"]
            if destination in self.files:
                assert self.fork_origins[destination] == workspace
            else:
                self.files[destination] = self.files[workspace]
                self.fork_origins[destination] = workspace
            return {"workspace_id": destination}
        if operation == "archive":
            if workspace not in self.files:
                raise HTTPException(404, "Workspace not found")
            return self.files[workspace]
        if operation == "restore":
            self.files[workspace] = base64.b64decode(kwargs["json"]["archive_base64"])
            return {}
        if operation == "tools":
            assert kwargs["json"]["name"] == "get_diff"
            if workspace in self.incomplete:
                raise HTTPException(422, "Git baseline is not initialized")
            return {"diff": ""}
        raise AssertionError((method, path))


def install(monkeypatch, task):
    session = Session(task)
    events = []
    monkeypatch.setattr(api, "owned", AsyncMock(return_value=task))
    monkeypatch.setattr(api, "public", lambda value: copy.deepcopy(vars(value)))

    async def record(_session, _task_id, kind, payload):
        events.append((kind, copy.deepcopy(payload)))

    monkeypatch.setattr(api, "record", record)
    return session, events


async def test_interrupted_baseline_retries_frozen_initial_content(monkeypatch):
    task = new_task(seed_workspace_id=str(uuid4()))
    session, events = install(monkeypatch, task)
    workspaces = Workspaces(task)
    monkeypatch.setattr(api, "broker", workspaces)
    observed = []

    async def verify(workspace, _command):
        observed.append(workspaces.files[workspace])
        if len(observed) == 1:
            raise HTTPException(503, "Injected interruption during initial baseline")
        return {"exit_code": 1, "stdout": observed[-1].decode()}

    monkeypatch.setattr(api, "verify_workspace", verify)
    with pytest.raises(HTTPException, match="Injected interruption"):
        await api.source(UUID(task.id), TOKEN, task.owner, session)
    assert session.database["workspace_ready"] is False
    await session.rollback()
    # The failed parent may resume, and a stale partial workspace may exist.
    # Neither is permission to replace the task's already frozen initial state.
    del workspaces.files[task.seed_workspace_id]  # Parent can even be unavailable.
    workspaces.files[task.id] = b"partially modified workspace"
    response = await api.source(UUID(task.id), TOKEN, task.owner, session)
    assert response.body == b"parent before"
    assert observed == [b"parent before", b"parent before"]
    assert session.database["workspace_ready"] is True
    assert session.database["baseline_test"]["stdout"] == "parent before"
    assert [kind for kind, _ in events] == ["test.baseline"]


async def test_legacy_ready_without_baseline_is_explicitly_unavailable(monkeypatch):
    task = new_task(workspace_ready=True)
    session, _ = install(monkeypatch, task)
    broker, verify = AsyncMock(), AsyncMock()
    monkeypatch.setattr(api, "broker", broker)
    monkeypatch.setattr(api, "verify_workspace", verify)
    with pytest.raises(HTTPException) as error:
        await api.source(UUID(task.id), TOKEN, task.owner, session)
    assert error.value.status_code == 409
    assert "baseline unavailable" in error.value.detail
    broker.assert_not_awaited()
    verify.assert_not_awaited()


async def test_incomplete_initial_snapshot_is_rejected_before_restoring_task(monkeypatch):
    task = new_task()
    session, _ = install(monkeypatch, task)
    workspaces = Workspaces(task)
    initial_id = str(uuid5(UUID(task.id), "coding-initial-workspace-v1"))
    workspaces.files[initial_id] = b"only some files survived initialization"
    workspaces.incomplete.add(initial_id)
    monkeypatch.setattr(api, "broker", workspaces)
    verify = AsyncMock()
    monkeypatch.setattr(api, "verify_workspace", verify)
    with pytest.raises(HTTPException) as error:
        await api.source(UUID(task.id), TOKEN, task.owner, session)
    assert error.value.status_code == 422
    assert task.id not in workspaces.files
    assert all(not path.endswith(("/restore", "/fork")) for _, path in workspaces.calls)
    assert session.database["workspace_ready"] is False
    verify.assert_not_awaited()


async def test_ready_workspace_retains_modified_files_and_saved_baseline(monkeypatch):
    task = new_task(workspace_ready=True, baseline_test={"exit_code": 1})
    session, _ = install(monkeypatch, task)
    workspaces = Workspaces(task)
    workspaces.files[task.id] = b"reviewable modifications"
    verify = AsyncMock()
    monkeypatch.setattr(api, "broker", workspaces)
    monkeypatch.setattr(api, "verify_workspace", verify)
    response = await api.source(UUID(task.id), TOKEN, task.owner, session)
    assert response.body == b"reviewable modifications"
    assert workspaces.calls == [("GET", f"/workspaces/{task.id}/archive")]
    verify.assert_not_awaited()


async def test_cancel_during_baseline_does_not_publish_workspace(monkeypatch):
    task = new_task()
    session, _ = install(monkeypatch, task)
    monkeypatch.setattr(api, "broker", Workspaces(task))

    async def verify(*_args):
        session.database["cancel_requested"] = True
        return {"exit_code": 0}

    monkeypatch.setattr(api, "verify_workspace", verify)
    with pytest.raises(HTTPException) as error:
        await api.source(UUID(task.id), TOKEN, task.owner, session)
    assert error.value.status_code == 409
    assert session.database["workspace_ready"] is False
    assert session.database["baseline_test"] is None


@pytest.mark.parametrize("exit_code", [0, 1])
async def test_cancel_committed_at_finalization_wins_without_test_command(monkeypatch, exit_code):
    task = new_task(workspace_ready=True, test_command="")
    session, _ = install(monkeypatch, task)
    monkeypatch.setattr(api, "broker", AsyncMock(return_value={"diff": "+changed"}))
    session.refresh_hook = lambda database: database.database.update(cancel_requested=True)
    result = await api.complete(UUID(task.id), api.Completion(lease_token=TOKEN, exit_code=exit_code), task.owner, session)
    assert result["status"] == "cancelled"
    assert session.database["status"] == "cancelled"
    assert result["diff"] == "+changed"


async def test_verification_exception_does_not_override_concurrent_cancellation(monkeypatch):
    task = new_task(workspace_ready=True, test_command="")
    session, events = install(monkeypatch, task)

    async def broker(*_args, **_kwargs):
        session.database["cancel_requested"] = True
        raise HTTPException(503, "Injected broker interruption")

    monkeypatch.setattr(api, "broker", broker)
    result = await api.complete(UUID(task.id), api.Completion(lease_token=TOKEN, exit_code=0), task.owner, session)
    assert result["status"] == "cancelled"
    assert result["error"] == "Injected broker interruption"
    assert [kind for kind, _ in events] == ["verification.error", "task.finished"]


async def test_final_refresh_keeps_this_requests_artifact_results(monkeypatch):
    task = new_task(workspace_ready=True)
    session, events = install(monkeypatch, task)
    monkeypatch.setattr(api, "broker", AsyncMock(return_value={"diff": "+actual change", "truncated": True}))
    monkeypatch.setattr(api, "verify_workspace", AsyncMock(return_value={"exit_code": 0, "stdout": "PASSED"}))
    result = await api.complete(UUID(task.id), api.Completion(lease_token=TOKEN, exit_code=0), task.owner, session)
    assert result["status"] == "awaiting_review"
    assert result["verification"] == {"exit_code": 0, "stdout": "PASSED"}
    assert result["diff"] == "+actual change"
    assert [kind for kind, _ in events] == ["test.started", "artifact.truncated", "test.completed", "task.finished"]


@pytest.mark.parametrize("fails", [False, True])
async def test_old_completion_cannot_overwrite_replacement_lease(monkeypatch, fails):
    task = new_task(workspace_ready=True)
    session, events = install(monkeypatch, task)
    monkeypatch.setattr(api, "broker", AsyncMock(return_value={"diff": "+old request"}))

    async def verify(*_args):
        # Reap -> resume -> claim -> complete can bring status back to verifying
        # while belonging to a different generation (the ABA case).
        session.database.update(
            lease_hash=hashlib.sha256(b"replacement-lease").hexdigest(),
            verification={"exit_code": 7}, diff="replacement diff", summary="replacement run",
        )
        if fails:
            raise HTTPException(503, "Old verification failed late")
        return {"exit_code": 0}

    monkeypatch.setattr(api, "verify_workspace", verify)
    with pytest.raises(HTTPException) as error:
        await api.complete(UUID(task.id), api.Completion(lease_token=TOKEN, exit_code=0), task.owner, session)
    assert error.value.status_code == 409
    assert session.database["status"] == "verifying"
    assert session.database["verification"] == {"exit_code": 7}
    assert session.database["diff"] == "replacement diff"
    assert [kind for kind, _ in events] == ["test.started"]

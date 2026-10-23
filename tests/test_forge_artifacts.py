"""Real SQLAlchemy/ASGI artifact contracts, plus two independent worker stores.

SQLite is used only for deterministic functional cases; this suite does not
claim PostgreSQL row-lock/concurrent-quota coverage. Production uses PostgreSQL.
"""

import copy
import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, Request
from sqlalchemy import create_engine, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from agent_backend.coding import api as coding_api
from agent_backend.coding.models import Artifact, Event, Task
from agent_backend.forge import api as forge_api
from agent_backend.forge import artifacts as api
from agent_backend.forge.artifact_client import ArtifactPersistenceError, ServerArtifacts
from agent_backend.forge.artifact_data import MAX_ARTIFACT_BYTES, artifact_id, describe_artifact
from agent_backend.forge.context import ContextBuilder

OWNER, LEASE = "artifact-owner", "test-artifact-lease"


@compiles(JSONB, "sqlite")
def sqlite_json(_type, _compiler, **_kwargs):
    return "JSON"


class AsyncFacade:
    def __init__(self, session):
        self.session = session

    async def scalar(self, statement):
        return self.session.scalar(statement)

    async def execute(self, statement):
        return self.session.execute(statement)

    async def get(self, model, key):
        return self.session.get(model, key)

    def add(self, value):
        self.session.add(value)

    async def commit(self):
        self.session.commit()


@pytest.fixture
async def backend(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    for table in (Task.__table__, Event.__table__, Artifact.__table__):
        table.create(engine)
    task_id = str(uuid4())
    with Session(engine) as session:
        session.add(Task(id=task_id, owner=OWNER, project_id=str(uuid4()), project_name="fixture", project_version=1,
                         source_workspace_id=str(uuid4()), prompt="inspect", engine="forge", status="running", workspace_ready=True,
                         lease_hash=hashlib.sha256(LEASE.encode()).hexdigest(),
                         lease_expires_at=datetime.now(UTC) + timedelta(minutes=5)))
        session.commit()
    # SQLite drops tzinfo; retain the real lease comparison/hash logic.
    monkeypatch.setattr(coding_api, "now", lambda: datetime.now(UTC).replace(tzinfo=None))
    app = FastAPI()
    app.include_router(api.router)
    app.include_router(forge_api.router)

    async def sessions():
        with Session(engine, expire_on_commit=False) as session:
            yield AsyncFacade(session)

    def identity(request: Request):
        return request.headers.get("X-Test-Owner", OWNER)

    app.dependency_overrides[api.get_session] = sessions
    app.dependency_overrides[api.identity] = identity
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        yield SimpleNamespace(client=client, engine=engine, task_id=task_id, app=app,
                              root=f"/api/coding/tasks/{task_id}")
    engine.dispose()


def state_for(store, raw):
    observation, reference = ContextBuilder(artifact_store=store).observation({"role": "tool", "tool_call_id": "step-1", "content": raw})
    return {"messages": [observation]}, reference


def row_count(backend):
    with Session(backend.engine) as session:
        return session.scalar(select(func.count()).select_from(Artifact))


async def checkpoint(backend, state, revision=0, lease=LEASE):
    return await backend.client.put(backend.root + "/checkpoint", json={"lease_token": lease, "expected_revision": revision, "state": state})


async def test_second_worker_recovers_full_observation_from_server_after_lease_rotation(backend, tmp_path):
    raw = "第一个worker的原文🙂\n" * 1500
    first = ServerArtifacts(backend.client, backend.task_id, LEASE, legacy_directory=tmp_path / "worker-a")
    state, reference = state_for(first, raw)
    # Referencing uncommitted bytes must fail, even with a valid lease/CAS.
    assert (await checkpoint(backend, state)).status_code == 409
    await first.prepare_checkpoint(state)
    assert (await checkpoint(backend, state)).status_code == 200
    assert len(state["messages"][0]["content"]) < len(raw)
    second_lease = "new-worker-lease"
    with Session(backend.engine) as session:
        task = session.get(Task, backend.task_id)
        task.lease_hash = hashlib.sha256(second_lease.encode()).hexdigest()
        session.commit()
    second = ServerArtifacts(backend.client, backend.task_id, second_lease, legacy_directory=tmp_path / "empty-worker-b")
    saved = await backend.client.get(backend.root + "/checkpoint", headers={"X-Lease-Token": second_lease})
    assert saved.status_code == 200
    await second.prepare_checkpoint(saved.json()["state"])
    offset, chunks = 0, []
    while offset is not None:
        page = await second.read(reference.uri, offset)
        assert page["offset_unit"] == "unicode_characters"
        chunks.append(page["content"])
        offset = page["next_offset"]
    assert "".join(chunks) == raw
    assert hashlib.sha256("".join(chunks).encode()).hexdigest() == reference.sha256
    assert row_count(backend) == 1
    assert not (tmp_path / "empty-worker-b").exists()
    with pytest.raises(ArtifactPersistenceError, match="409"):
        await first.read(reference.uri)


async def test_owner_and_task_namespace_apply_even_when_digest_is_known(backend):
    store = ServerArtifacts(backend.client, backend.task_id, LEASE)
    uri = store.put("private result", "tool_observation")
    await store.flush()
    route = backend.root + "/artifacts/" + artifact_id(uri)
    for method in ("GET", "PUT"):
        kwargs = {"json": {"kind": "tool_observation", "content": "private result"}} if method == "PUT" else {}
        response = await backend.client.request(method, route, headers={"X-Test-Owner": "other-owner", "X-Lease-Token": LEASE}, **kwargs)
        assert response.status_code == 404
    other_task = route.replace(backend.task_id, str(uuid4()))
    assert (await backend.client.get(other_task, headers={"X-Lease-Token": LEASE})).status_code == 404
    assert row_count(backend) == 1


@pytest.mark.parametrize("change", ["expired", "cancelled", "not_ready", "wrong_engine", "old_token"])
async def test_writes_are_fenced_before_idempotent_duplicate_returns(backend, change):
    store = ServerArtifacts(backend.client, backend.task_id, LEASE)
    uri = store.put("same bytes", "tool_observation")
    await store.flush()
    with Session(backend.engine) as session:
        task = session.get(Task, backend.task_id)
        if change == "expired":
            task.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        elif change == "cancelled":
            task.cancel_requested = True
        elif change == "not_ready":
            task.workspace_ready = False
        elif change == "wrong_engine":
            task.engine = "codex"
        else:
            task.lease_hash = "0" * 64
        session.commit()
    response = await backend.client.put(backend.root + "/artifacts/" + artifact_id(uri),
                                        headers={"X-Lease-Token": LEASE}, json={"kind": "tool_observation", "content": "same bytes"})
    assert response.status_code == 409
    assert row_count(backend) == 1


async def test_put_is_idempotent_after_committed_upload_response_is_lost(backend):
    transport = httpx.ASGITransport(backend.app)

    class LostResponse(httpx.AsyncBaseTransport):
        dropped = False

        async def handle_async_request(self, request):
            response = await transport.handle_async_request(request)
            if request.method == "PUT" and not self.dropped:
                self.dropped = True
                await response.aclose()
                raise httpx.ReadError("injected lost acknowledgement")
            return response

    async with httpx.AsyncClient(transport=LostResponse(), base_url="http://test") as client:
        store = ServerArtifacts(client, backend.task_id, LEASE)
        uri = store.put("committed before dropped response", "tool_observation")
        await store.flush()
        assert not store.pending
        assert (await store.read(uri))["content"] == "committed before dropped response"
    assert row_count(backend) == 1


async def test_content_hash_size_and_database_quota_checks(backend, monkeypatch):
    headers = {"X-Lease-Token": LEASE}
    response = await backend.client.put(backend.root + "/artifacts/" + "0" * 64, headers=headers,
                                        json={"kind": "tool_observation", "content": "hash mismatch"})
    assert response.status_code == 422
    # Character length passes Pydantic; UTF-8 byte length must still be checked.
    response = await backend.client.put(backend.root + "/artifacts/" + "0" * 64, headers=headers,
                                        json={"kind": "tool_observation", "content": "界" * (MAX_ARTIFACT_BYTES // 3 + 1)})
    assert response.status_code == 413
    monkeypatch.setattr(api, "MAX_TASK_ARTIFACT_BYTES", 7)
    store = ServerArtifacts(backend.client, backend.task_id, LEASE)
    store.put("界界", "tool_observation")  # 6 bytes, not 2.
    await store.flush()
    again = ServerArtifacts(backend.client, backend.task_id, LEASE)
    again.put("界界", "tool_observation")
    await again.flush()  # Idempotent duplicate does not consume another 6 bytes.
    again.put("xx", "tool_observation")
    with pytest.raises(ArtifactPersistenceError, match="413"):
        await again.flush()
    assert row_count(backend) == 1


async def test_item_count_quota_is_checked_independently(backend, monkeypatch):
    monkeypatch.setattr(api, "MAX_TASK_ARTIFACTS", 1)
    store = ServerArtifacts(backend.client, backend.task_id, LEASE)
    store.put("first", "tool_observation")
    await store.flush()
    store.put("second", "tool_observation")
    with pytest.raises(ArtifactPersistenceError, match="413"):
        await store.flush()
    assert row_count(backend) == 1


async def test_checkpoint_metadata_cannot_relabel_or_forge_persisted_bytes(backend):
    store = ServerArtifacts(backend.client, backend.task_id, LEASE)
    state, _ = state_for(store, "correct original")
    await store.prepare_checkpoint(state)
    altered = copy.deepcopy(state)
    altered["messages"][0]["_artifact_reference"]["sha256"] = "f" * 64
    assert (await checkpoint(backend, altered)).status_code == 422
    assert (await checkpoint(backend, state)).status_code == 200


async def test_legacy_migration_needs_exact_original_bytes_and_fails_closed_without_them(backend, tmp_path):
    raw = "old local observation\nsecond line"
    old = ServerArtifacts(backend.client, backend.task_id, LEASE)
    state, reference = state_for(old, raw)
    migrating = ServerArtifacts(backend.client, backend.task_id, LEASE, legacy_directory=tmp_path)
    with pytest.raises(ArtifactPersistenceError, match="LEGACY_ARTIFACT_UNAVAILABLE"):
        await migrating.prepare_checkpoint(state)
    path = tmp_path / f"{artifact_id(reference.uri)}.txt"
    path.write_text("tampered", encoding="utf-8")
    with pytest.raises(ArtifactPersistenceError, match="INTEGRITY"):
        await migrating.prepare_checkpoint(state)
    path.write_text(raw, encoding="utf-8")
    await migrating.prepare_checkpoint(state)
    assert (await migrating.read(reference.uri))["content"] == raw
    assert row_count(backend) == 1


async def test_failed_upload_never_publishes_checkpoint_or_drops_staged_bytes(backend):
    store = ServerArtifacts(backend.client, backend.task_id, "obsolete lease")
    state, _ = state_for(store, "must survive until acknowledged")
    with pytest.raises(ArtifactPersistenceError):
        await store.prepare_checkpoint(state)
    assert len(store.pending) == 1
    with Session(backend.engine) as session:
        task = session.get(Task, backend.task_id)
        assert task.checkpoint_revision == 0 and task.runtime_state is None
    assert row_count(backend) == 0


@pytest.mark.parametrize("value", ["artifact://../secret", "file:///secret", "artifact://" + "A" * 64])
def test_artifact_uri_is_not_an_arbitrary_path(value):
    with pytest.raises(ValueError):
        artifact_id(value)


def test_kind_and_raw_content_have_distinct_hash_semantics():
    first = describe_artifact("text", "tool_observation")
    second = describe_artifact("text", "other")
    assert first["uri"] != second["uri"]
    assert first["sha256"] == second["sha256"] == hashlib.sha256(b"text").hexdigest()

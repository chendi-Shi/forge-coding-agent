"""Deterministic lifecycle races against real PostgreSQL; zero model calls.

Runs API functions in a separate Python process inside this project's API
container. Broker/verification I/O is replaced by controlled barriers; this is
not an HTTP, real Docker execution, or worker-kill test. Only UUID-scoped rows
created by this invocation are removed. No running service is patched/restarted.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import importlib
import json
import os
import re
import subprocess
import time
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4


async def child(payload):
    from fastapi import HTTPException
    from sqlalchemy import URL, delete, text
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from agent_backend.coding.models import Event, Executor, Project, Task, ToolOperation, now

    api = importlib.import_module("agent_backend.coding.api")
    exec(compile(payload["api_source"], "<reviewed-coding-api>", "exec"), api.__dict__)
    url = URL.create("postgresql+asyncpg", username=os.environ["POSTGRES_USER"],
                     password=os.environ["POSTGRES_PASSWORD"], host=os.environ.get("POSTGRES_HOST", "postgres"),
                     port=int(os.environ.get("POSTGRES_PORT", "5432")), database=os.environ["POSTGRES_DB"])
    engine = create_async_engine(url, pool_size=6, max_overflow=2)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    owner = "lifecycle-probe-" + uuid4().hex
    task_ids, project_ids, checks = [], [], []
    world = {}
    broker_gate = None

    async def fake_broker(method, path, *, json=None, raw=False, **kwargs):
        nonlocal broker_gate
        parts = path.strip("/").split("/")
        identity = parts[1]
        operation = parts[2] if len(parts) > 2 else ""
        if operation == "fork":
            if broker_gate:
                reached, release = broker_gate
                broker_gate = None
                reached.set()
                await release.wait()
            target = json["workspace_id"]
            world.setdefault(target, world[identity])  # Preserve first frozen fork.
            return {"workspace_id": target}
        if operation == "archive":
            if identity not in world:
                raise HTTPException(404, "Controlled workspace not found")
            return world[identity]
        if operation == "restore":
            world[identity] = base64.b64decode(json["archive_base64"])
            return {"diff": "controlled diff"}
        if operation == "diff":
            return {"diff": "controlled diff", "truncated": False}
        if operation == "tools" and json.get("name") == "get_diff":
            if identity not in world:
                raise HTTPException(404, "Controlled workspace not found")
            return {"diff": "", "truncated": False}
        if method == "DELETE":
            world.pop(identity, None)
            return {}
        raise AssertionError("Unexpected controlled broker operation")

    async def normal_verify(identity, command):
        return {"exit_code": 0, "stdout": world[identity].decode(), "stderr": ""}

    api.broker, api.verify_workspace = fake_broker, normal_verify

    async def seed(*, ready=False, command="controlled-check", with_seed=False):
        identifier, project_id, source_id = str(uuid4()), str(uuid4()), str(uuid4())
        executor_id, token = "lifecycle-" + uuid4().hex, uuid4().hex
        task_ids.append(identifier)
        project_ids.append(project_id)
        world[source_id] = b"project-before"
        seed_id = str(uuid4()) if with_seed else None
        if seed_id:
            world[seed_id] = b"seed-before"
        if ready:
            world[identifier] = b"task-candidate"
        async with maker() as session:
            session.add(Project(id=project_id, owner=owner, name="isolated lifecycle probe", workspace_id=source_id))
            session.add(Executor(id=executor_id, owner=owner, capacity=1))
            session.add(Task(id=identifier, owner=owner, project_id=project_id, project_name="isolated lifecycle probe",
                             project_version=1, source_workspace_id=source_id, seed_workspace_id=seed_id,
                             workspace_ready=ready, prompt="No model: controlled lifecycle check", engine="forge",
                             status="running", executor_id=executor_id, test_command=command,
                             lease_hash=hashlib.sha256(token.encode()).hexdigest(),
                             lease_expires_at=now() + timedelta(minutes=10)))
            await session.commit()
        return identifier, token, executor_id, seed_id

    async def snapshot(identifier):
        async with maker() as session:
            task = await session.get(Task, identifier)
            return {"status": task.status, "cancel_requested": task.cancel_requested,
                    "workspace_ready": task.workspace_ready, "baseline_test": task.baseline_test}

    async def invoke_source(identifier, token):
        async with maker() as session:
            return await api.source(UUID(identifier), token, owner, session)

    async def cancel(identifier, session=None):
        if session:
            return await api.cancel(UUID(identifier), owner, session)
        async with maker() as current:
            return await api.cancel(UUID(identifier), owner, current)

    async def wait_for_db_outcome(operation, pid):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if operation.done():
                await operation
                return "committed"
            async with maker() as observer:
                wait = await observer.scalar(text("SELECT wait_event_type FROM pg_stat_activity WHERE pid=:pid"), {"pid": pid})
            if wait == "Lock":
                return "postgres_lock_wait"
            await asyncio.sleep(0.02)
        raise AssertionError("No deterministic PostgreSQL concurrency outcome")

    async def run_case(name, function):
        try:
            details = await asyncio.wait_for(function(), timeout=25)
            checks.append({"name": name, **details})
        except Exception as exc:
            # Never serialize DB exceptions, query parameters or credentials.
            checks.append({"name": name, "passed": False, "error_type": type(exc).__name__})
        finally:
            api.verify_workspace = normal_verify

    async def source_retry():
        identifier, token, _, seed_id = await seed(with_seed=True)
        observed = []

        async def interrupted(identity, command):
            observed.append(world[identity].decode())
            raise RuntimeError("Injected verification interruption")

        api.verify_workspace = interrupted
        try:
            await invoke_source(identifier, token)
        except RuntimeError:
            pass
        before = await snapshot(identifier)
        world.pop(seed_id)  # The original parent may disappear after freezing.
        world[identifier] = b"task-edited-later"
        api.verify_workspace = normal_verify
        await invoke_source(identifier, token)
        after = await snapshot(identifier)
        baseline = after["baseline_test"] or {}
        return {"passed": not before["workspace_ready"] and before["baseline_test"] is None
                and after["workspace_ready"] and baseline.get("stdout") == "seed-before",
                "ready_after_interruption": before["workspace_ready"], "retry_baseline": baseline.get("stdout"),
                "first_baseline_observation": observed, "original_seed_missing_on_retry": seed_id not in world}

    async def source_heartbeat():
        nonlocal broker_gate
        identifier, token, executor_id, _ = await seed()
        reached, release = asyncio.Event(), asyncio.Event()
        broker_gate = reached, release
        operation = asyncio.create_task(invoke_source(identifier, token))
        try:
            await reached.wait()
            async with maker() as session:
                pid = await session.scalar(text("SELECT pg_backend_pid()"))
                beat = asyncio.create_task(api.heartbeat(api.Heartbeat(executor_id=executor_id, task_id=identifier,
                                                                      lease_token=token), owner, session))
                try:
                    outcome = await wait_for_db_outcome(beat, pid)
                finally:
                    release.set()
                await beat
            await operation
            return {"passed": outcome == "committed", "heartbeat_during_fork": outcome}
        finally:
            release.set()
            if not operation.done():
                operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)

    async def legacy_missing_baseline():
        identifier, token, _, _ = await seed(ready=True)
        try:
            await invoke_source(identifier, token)
        except HTTPException as exc:
            return {"passed": exc.status_code == 409, "status_code": exc.status_code}
        return {"passed": False, "status_code": 200}

    async def completion_after_refresh():
        identifier, token, _, _ = await seed(ready=True, command="")
        reached, release = asyncio.Event(), asyncio.Event()

        class PausedSession(AsyncSession):
            async def refresh(self, *args, **kwargs):
                await super().refresh(*args, **kwargs)
                reached.set()
                await release.wait()

        async def finish():
            async with PausedSession(bind=engine, expire_on_commit=False) as session:
                return await api.complete(UUID(identifier), api.Completion(lease_token=token, exit_code=0), owner, session)

        operation = asyncio.create_task(finish())
        try:
            await reached.wait()
            async with maker() as session:
                pid = await session.scalar(text("SELECT pg_backend_pid()"))
                cancelling = asyncio.create_task(cancel(identifier, session))
                try:
                    outcome = await wait_for_db_outcome(cancelling, pid)
                finally:
                    release.set()
                await operation
                await cancelling
            final = await snapshot(identifier)
            return {"passed": outcome == "postgres_lock_wait" and final["status"] == "awaiting_review"
                    and not final["cancel_requested"], "cancel_after_final_refresh": outcome, "final": final}
        finally:
            release.set()
            if not operation.done():
                operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)

    async def verification_error_cancel():
        identifier, token, _, _ = await seed(ready=True)
        reached, release = asyncio.Event(), asyncio.Event()

        async def failing_verify(identity, command):
            reached.set()
            await release.wait()
            raise HTTPException(503, "Injected controlled verification failure")

        api.verify_workspace = failing_verify

        async def finish():
            async with maker() as session:
                return await api.complete(UUID(identifier), api.Completion(lease_token=token, exit_code=0), owner, session)

        operation = asyncio.create_task(finish())
        try:
            await reached.wait()
            await cancel(identifier)
            release.set()
            await operation
            final = await snapshot(identifier)
            return {"passed": final["status"] == "cancelled", "final": final}
        finally:
            release.set()
            if not operation.done():
                operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)

    try:
        await run_case("source_baseline_interrupt_and_frozen_retry", source_retry)
        await run_case("source_external_io_does_not_block_heartbeat", source_heartbeat)
        await run_case("legacy_ready_without_baseline_is_not_relabelled", legacy_missing_baseline)
        await run_case("complete_final_refresh_serializes_cancel", completion_after_refresh)
        await run_case("verification_error_cannot_overwrite_cancel", verification_error_cancel)
    finally:
        async with maker() as session:
            # Exact generated IDs and owner; never delete unrelated application data.
            if task_ids:
                await session.execute(delete(Event).where(Event.task_id.in_(task_ids)))
                await session.execute(delete(ToolOperation).where(ToolOperation.task_id.in_(task_ids)))
                await session.execute(delete(Task).where(Task.owner == owner, Task.id.in_(task_ids)))
            await session.execute(delete(Executor).where(Executor.owner == owner))
            if project_ids:
                await session.execute(delete(Project).where(Project.owner == owner, Project.id.in_(project_ids)))
            await session.commit()
        await engine.dispose()
    return {"passed": all(check["passed"] for check in checks), "checks": checks,
            "evidence_type": "real_postgresql_api_functions_with_controlled_broker_and_verification_barriers",
            "http_test": False, "real_docker_tool_execution": False, "worker_killed": False,
            "real_model_calls": 0, "synthetic_rows_cleaned": True,
            "api_source_ref": payload["api_ref"], "api_source_sha256": hashlib.sha256(payload["api_source"].encode()).hexdigest()}


def main():
    from prepare_issue_benchmarks import docker_prefix

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-ref", default="WORKTREE", help="WORKTREE or a local Git revision, for before/after evidence")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--docker-prefix", help="JSON argv override; defaults to this project's runtime")
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    source_path = "src/agent_backend/coding/api.py"
    source = ((project / source_path).read_text(encoding="utf-8") if args.api_ref == "WORKTREE" else
              subprocess.check_output(["git", "show", f"{args.api_ref}:{source_path}"], cwd=project).decode())
    prefix = docker_prefix(args.docker_prefix)
    candidates = subprocess.check_output([*prefix, "ps", "--filter", "label=com.docker.compose.project=agent-backend-aegra",
                                          "--filter", "label=com.docker.compose.service=api", "--filter", "status=running",
                                          "--format", "{{.ID}}"], text=True).splitlines()
    if not candidates or not re.fullmatch(r"[a-f0-9]{12,64}", candidates[0]):
        raise SystemExit("No running API container belonging to this project")
    target = candidates[0]
    labels = json.loads(subprocess.check_output([*prefix, "inspect", "--format", "{{json .Config.Labels}}", target], text=True))
    if labels.get("com.docker.compose.project") != "agent-backend-aegra" or labels.get("com.docker.compose.service") != "api":
        raise SystemExit("API container ownership check failed")
    payload = {"script": Path(__file__).read_text(encoding="utf-8"), "api_source": source, "api_ref": args.api_ref}
    bootstrap = "import json,sys; p=json.load(sys.stdin); exec(compile(p['script'],'<lifecycle-probe>','exec'), {'__name__':'__lifecycle_child__','PAYLOAD':p})"
    result = subprocess.run([*prefix, "exec", "-i", target, "python", "-c", bootstrap], input=json.dumps(payload).encode(),
                            capture_output=True, timeout=160)
    if result.returncode:
        raise SystemExit("Lifecycle child failed; raw diagnostics intentionally withheld to protect connection details")
    report = json.loads(result.stdout.splitlines()[-1])
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__lifecycle_child__":
    print(json.dumps(asyncio.run(child(globals()["PAYLOAD"]))))
elif __name__ == "__main__":
    raise SystemExit(main())

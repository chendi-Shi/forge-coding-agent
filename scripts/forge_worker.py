#!/usr/bin/env python3
"""Run Forge decisions locally; execute all repository actions in Docker via API."""

import asyncio
import hashlib
import os
import re
import signal
import sys
import time
from dataclasses import asdict
from pathlib import Path

import httpx
from coding_worker import Backend, CodingJob, EventJournal, WorkerError, log, parse_args, read_auth_token
from dotenv import dotenv_values

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
PROJECT_ROOT = Path(__file__).resolve().parents[1]

from agent_backend.forge.protocol import MUTATING_TOOLS, digest  # noqa: E402
from agent_backend.forge.providers import CodexDecisionProvider, OpenAICompatibleProvider, ProviderError  # noqa: E402
from agent_backend.forge.retrieval import CodeIndex  # noqa: E402
from agent_backend.forge.runtime import AgentRuntime, RuntimeFailure  # noqa: E402


class FileArtifacts:
    def __init__(self, directory):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)

    def put(self, text, kind):
        key = hashlib.sha256((kind + "\0" + text).encode()).hexdigest()
        path = self.directory / f"{key}.txt"
        if not path.exists():
            path.write_text(text, encoding="utf-8")
        return "artifact://" + key

    def read(self, uri, offset=0):
        if not re.fullmatch(r"artifact://[a-f0-9]{64}", uri):
            return {"error": "Invalid artifact URI"}
        path = self.directory / (uri.removeprefix("artifact://") + ".txt")
        if not path.exists():
            return {"error": "Artifact is on the original worker; use read_file or rerun a read-only inspection"}
        value = path.read_text(encoding="utf-8")
        return {"uri": uri, "offset": offset, "content": value[offset:offset + 8000], "next_offset": offset + 8000 if len(value) > offset + 8000 else None}


class ForgeJob(CodingJob):
    async def request(self, method, suffix, **kwargs):
        response = await self.backend.client.request(method, f"/api/coding/tasks/{self.task_id}{suffix}", **kwargs)
        if response.is_error:
            raise WorkerError(f"BACKEND_HTTP_{response.status_code}")
        return response.json()

    async def run(self):
        self.directory = self.directory.parent / self.task_id
        self.directory.mkdir(parents=True, exist_ok=True)
        artifacts = FileArtifacts(self.directory / "artifacts")
        heartbeat = asyncio.create_task(self.heartbeat())
        streaming = None
        state = None
        error = None
        try:
            async with asyncio.timeout(self.task["timeout_seconds"]):
                source = await self.cancellable(self.backend.source(self.task_id, self.lease))
                saved = await self.request("GET", "/checkpoint", headers={"X-Lease-Token": self.lease})
                self.revision, state = saved["revision"], saved["state"]
                self.journal = EventJournal(self.directory / "events.jsonl", self.secrets)
                self.journal.sequence = saved["event_sequence"]
                streaming = asyncio.create_task(self.stream_events())
                index = CodeIndex.from_zip(source, source_version=hashlib.sha256(source).hexdigest()[:16])
                dirty = False

                async def emit(event):
                    self.journal.append(event)

                async def checkpoint(value):
                    nonlocal state
                    state = value
                    body = {"lease_token": self.lease, "expected_revision": self.revision, "state": value}
                    try:
                        result = await self.request("PUT", "/checkpoint", json=body)
                        self.revision = result["revision"]
                    except (httpx.HTTPError, WorkerError):
                        # Reconcile a lost acknowledgement without duplicating a revision.
                        actual = await self.request("GET", "/checkpoint", headers={"X-Lease-Token": self.lease})
                        if actual["revision"] == self.revision + 1 and digest(actual["state"]) == digest(value):
                            self.revision = actual["revision"]
                        else:
                            raise

                async def execute(call_id, name, arguments):
                    nonlocal index, dirty
                    if name == "repository_search":
                        if dirty:
                            current = await self.backend.source(self.task_id, self.lease)
                            index = CodeIndex.from_zip(current, source_version=hashlib.sha256(current).hexdigest()[:16])
                            dirty = False
                        return asdict(index.search(arguments["query"], limit=arguments.get("limit", 5), max_tokens=2600))
                    if name == "read_artifact":
                        return artifacts.read(**arguments)
                    result = await self.request("POST", "/tools", json={"lease_token": self.lease, "call_id": call_id, "name": name, "arguments": arguments}, timeout=300)
                    dirty |= name in MUTATING_TOOLS
                    return result["result"]

                if os.environ.get("FORGE_PROVIDER", "codex_bridge") == "openai_compatible":
                    provider = OpenAICompatibleProvider(base_url=os.environ.get("FORGE_BASE_URL", ""), api_key=os.environ.get("FORGE_API_KEY", ""), model=os.environ.get("FORGE_MODEL"),
                                                        token_parameter=os.environ.get("FORGE_TOKEN_PARAMETER", "max_completion_tokens"))
                else:
                    provider = CodexDecisionProvider(executable=self.codex, work_dir=self.directory / "decisions", timeout_seconds=120)
                runtime = AgentRuntime(provider=provider, execute=execute, checkpoint=checkpoint, emit=emit, artifacts=artifacts, model=self.task.get("model"))
                remaining = (state or {}).get("deadline", time.time() + self.task["timeout_seconds"]) - time.time()
                async with asyncio.timeout(max(0.001, remaining)):
                    state = await self.cancellable(runtime.run(self.task, state))
        except (ProviderError, RuntimeFailure, WorkerError) as exc:
            error = str(exc)
        except TimeoutError:
            error = "TASK_TIME_BUDGET_EXHAUSTED"
        except asyncio.CancelledError:
            error = "WORKER_STOPPED"
        except Exception as exc:
            error = type(exc).__name__
        finally:
            if streaming:
                streaming.cancel()
                await asyncio.gather(streaming, return_exceptions=True)
            if self.journal:
                self.journal.append({"type": "runtime.failed" if error else "runtime.finished", "error": error})
                try:
                    await self.journal.upload(self.backend, self.task_id, self.lease)
                except (WorkerError, httpx.HTTPError):
                    pass
                self.journal.close()
            try:
                await self.backend.post(f"/tasks/{self.task_id}/complete", {
                    "lease_token": self.lease, "exit_code": 1 if error else 0,
                    "summary": (state or {}).get("summary", ""), "error": error,
                    "usage": {**(state or {}).get("usage", {}), "provider_calls": (state or {}).get("provider_calls", 0),
                              "model_seconds": (state or {}).get("model_seconds", 0), "engine": "forge"},
                }, timeout=300)
            except (WorkerError, httpx.HTTPError) as exc:
                log("completion_unavailable", task_id=self.task_id, error=str(exc) if isinstance(exc, WorkerError) else type(exc).__name__)
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            log("forge_job_finished", task_id=self.task_id, error=error)


async def worker(args):
    settings = dotenv_values(PROJECT_ROOT / ".env")
    for name in ("FORGE_PROVIDER", "FORGE_BASE_URL", "FORGE_API_KEY", "FORGE_MODEL", "FORGE_TOKEN_PARAMETER"):
        if settings.get(name):
            os.environ.setdefault(name, settings[name])
    token = read_auth_token()
    workspaces = await asyncio.to_thread(Path(args.workspaces).resolve)
    workspaces.mkdir(parents=True, exist_ok=True)
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    body = {"executor_id": args.executor_id, "capacity": args.concurrency, "version": "forge-2.0.0", "engine": "forge"}
    async with httpx.AsyncClient(base_url=args.base_url, headers={"Authorization": "Bearer " + token}, timeout=20, trust_env=False) as client:
        backend, active = Backend(client), {}
        registered = 0
        try:
            while not stop.is_set() and not (args.stop_file and await asyncio.to_thread(Path(args.stop_file).exists)):
                active = {future: job for future, job in active.items() if not future.done()}
                try:
                    if time.monotonic() - registered > 15:
                        await backend.post("/executors/register", body)
                        registered = time.monotonic()
                    if len(active) < args.concurrency:
                        response = await backend.post("/executors/claim", body)
                        if response.get("task"):
                            job = ForgeJob(backend, response["task"], args.executor_id, workspaces, args.codex_executable, token)
                            active[asyncio.create_task(job.run())] = job
                            continue
                except WorkerError as exc:
                    log("forge_poll_error", error=str(exc))
                await asyncio.sleep(1)
        finally:
            for job in active.values():
                job.cancel("WORKER_STOPPED")
            await asyncio.gather(*active, return_exceptions=True)


if __name__ == "__main__":
    options = parse_args()
    if "--workspaces" not in sys.argv:
        options.workspaces = str(Path(__file__).resolve().parents[1] / ".forge-workspaces")
    asyncio.run(worker(options))

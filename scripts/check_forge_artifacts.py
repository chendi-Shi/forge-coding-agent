"""Real HTTP/PostgreSQL artifacts across fresh clients and a replaced task lease.

Only newly created isolation-test tasks are changed. No model calls, simulated
provider throughput, or claim of physically separate hosts. No legacy files are
available to the fresh client. Run separately from other queue probes.
"""

import asyncio
import copy
import hashlib
import json
import time
from pathlib import Path
from uuid import uuid4

import httpx
from dotenv import dotenv_values

from agent_backend.forge.artifact_client import ServerArtifacts
from agent_backend.forge.context import ContextBuilder

ROOT = Path(__file__).resolve().parents[1]


async def main():
    keys = json.loads(dotenv_values(ROOT / ".env")["API_KEYS_JSON"])
    headers = {"Authorization": "Bearer " + keys["isolation-test"]}
    report = {"passed": False, "checks": [], "real_model_calls": 0,
              "evidence_type": "real_http_postgresql_fresh_clients_and_lease_replacement",
              "physical_hosts": 1, "legacy_local_files_used": False}
    task_id = None
    cleanup_token = None
    async with httpx.AsyncClient(base_url="http://127.0.0.1:2026", headers=headers, trust_env=False, timeout=300) as first:
        async def post(path, body=None):
            response = await first.post("/api/coding" + path, json=body or {})
            response.raise_for_status()
            return response.json()

        def check(name, condition, **details):
            report["checks"].append({"name": name, "passed": bool(condition), **details})
            if not condition:
                raise AssertionError(name)

        try:
            response = await first.get("/api/coding/tasks")
            response.raise_for_status()
            existing = response.json()["tasks"]
            assert not any(t["engine"] == "forge" and t["status"] in {"queued", "running", "verifying"} for t in existing), "Finish other isolated Forge probes first"
            project = await post("/projects", {"name": "Durable artifact recovery probe", "files": {"app.py": "answer = 42\n"}, "test_command": "true"})
            task = await post("/tasks", {"project_id": project["id"], "engine": "forge", "prompt": "Verify durable observation recovery without model calls"})
            task_id = task["id"]
            registration = {"executor_id": "artifact-probe-" + uuid4().hex[:12], "engine": "forge", "capacity": 1}
            lease = (await post("/executors/claim", registration))["task"]
            assert lease["id"] == task_id
            token = lease["lease_token"]
            cleanup_token = token
            prefix = f"/api/coding/tasks/{task_id}"
            (await first.get(prefix + "/source", headers={"X-Lease-Token": token})).raise_for_status()
            original = ("跨 worker 原始观察 🌱\n" * 1500) + "END-OF-ORIGINAL-OBSERVATION"
            store = ServerArtifacts(first, task_id, token)
            # The provenance header itself needs more than 32 estimated tokens.
            context = ContextBuilder(artifact_store=store, observation_tokens=128)
            observation, reference = context.observation({"role": "tool", "tool_call_id": "probe", "content": original})
            state = {"version": 1, "deadline": time.time() + 600, "messages": [observation]}
            checkpoint = {"lease_token": token, "expected_revision": 0, "state": state}
            missing = await first.put(prefix + "/checkpoint", json=checkpoint)
            check("checkpoint_rejects_unpersisted_reference", missing.status_code == 409)
            await store.prepare_checkpoint(state)
            saved = await first.put(prefix + "/checkpoint", json=checkpoint)
            check("checkpoint_publishes_after_bytes", saved.status_code == 200 and saved.json()["revision"] == 1)
            key = reference.uri.removeprefix("artifact://")
            duplicate = await first.put(prefix + "/artifacts/" + key, headers={"X-Lease-Token": token}, json={"kind": reference.kind, "content": original})
            check("identical_put_is_idempotent", duplicate.status_code == 200 and duplicate.json()["created"] is False)
            mismatch = await first.put(prefix + "/artifacts/" + key, headers={"X-Lease-Token": token}, json={"kind": reference.kind, "content": "different"})
            check("hash_mismatch_rejected", mismatch.status_code == 422)
            forged = copy.deepcopy(state)
            forged["messages"][0]["_artifact_reference"]["sha256"] = "0" * 64
            bad = await first.put(prefix + "/checkpoint", json={"lease_token": token, "expected_revision": 1, "state": forged})
            check("checkpoint_metadata_mismatch_rejected", bad.status_code == 422)
            foreign = await first.get(prefix + "/artifacts/" + key, headers={"Authorization": "Bearer " + keys["local-client"], "X-Lease-Token": token})
            check("cross_owner_hidden", foreign.status_code == 404)
            too_large = "界" * 400000
            too_large_id = hashlib.sha256((reference.kind + "\0" + too_large).encode()).hexdigest()
            oversized = await first.put(prefix + "/artifacts/" + too_large_id, headers={"X-Lease-Token": token}, json={"kind": reference.kind, "content": too_large})
            check("utf8_byte_limit_rejected", oversized.status_code == 413)

            failed = await post(f"/tasks/{task_id}/complete", {"lease_token": token, "exit_code": 1, "error": "Controlled worker replacement"})
            assert failed["status"] == "failed"
            await post(f"/tasks/{task_id}/resume")
            lease = (await post("/executors/claim", {**registration, "executor_id": registration["executor_id"] + "-fresh"}))["task"]
            assert lease["id"] == task_id
            new_token = lease["lease_token"]
            cleanup_token = new_token
            stale = await first.get(prefix + "/artifacts/" + key, headers={"X-Lease-Token": token})
            check("old_lease_cannot_read", stale.status_code == 409)

            # A distinct HTTP client/store has no pending bytes, confirmed cache,
            # local artifact path or legacy_directory from the first execution.
            async with httpx.AsyncClient(base_url="http://127.0.0.1:2026", headers=headers, trust_env=False, timeout=30) as second:
                restored = ServerArtifacts(second, task_id, new_token)
                recovered = await second.get(prefix + "/checkpoint", headers={"X-Lease-Token": new_token})
                recovered.raise_for_status()
                await restored.prepare_checkpoint(recovered.json()["state"])
                parts, offset, pages = [], 0, 0
                while True:
                    value = await restored.read(reference.uri, offset)
                    assert value["offset"] == offset and value["offset_unit"] == "unicode_characters"
                    parts.append(value["content"])
                    pages += 1
                    if value["next_offset"] is None:
                        break
                    offset = value["next_offset"]
                joined = "".join(parts)
                check("fresh_worker_reads_original_without_local_files", joined == original and pages > 1,
                      pages=pages, utf8_bytes=len(joined.encode()), sha256=hashlib.sha256(joined.encode()).hexdigest())
                check("checkpoint_stays_bounded", len(json.dumps(recovered.json()["state"]).encode()) < len(original.encode()) // 4)
            await post(f"/tasks/{task_id}/complete", {"lease_token": new_token, "exit_code": 1, "error": "Controlled probe complete"})
            report.update(passed=True, task_id=task_id)
        except Exception as error:
            report["error_type"] = type(error).__name__
            raise
        finally:
            if task_id:
                report["task_id"] = task_id
                try:
                    remaining = await post(f"/tasks/{task_id}/cancel")
                    if remaining["status"] == "running" and cleanup_token:
                        await post(f"/tasks/{task_id}/complete", {"lease_token": cleanup_token, "exit_code": 1, "error": "Artifact probe cleanup"})
                except Exception as error:
                    report.update(passed=False, cleanup_error_type=type(error).__name__)
            output = ROOT / "results/forge-artifacts.json"
            output.parent.mkdir(exist_ok=True)
            output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())

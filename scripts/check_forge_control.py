"""Real PostgreSQL + Docker checks for Forge; zero model calls.

Includes a deliberately dropped HTTP response after a command may have started.
Never treats uncertain execution as permission to repeat a side effect.
"""

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]


async def main():
    keys = json.loads(dotenv_values(ROOT / ".env")["API_KEYS_JSON"])
    report = {"real_model_calls": 0}
    async with httpx.AsyncClient(base_url="http://127.0.0.1:2026/api/coding", headers={"Authorization": "Bearer " + keys["isolation-test"]}, timeout=300, trust_env=False) as client:
        async def post(path, body=None):
            response = await client.post(path, json=body or {})
            response.raise_for_status()
            return response.json()

        project = await post("/projects", {"name": "Forge recovery control " + uuid4().hex[:8], "files": {"app.py": "answer = 41\n"}, "test_command": 'python -c "from app import answer; assert answer == 42"'})
        task = await post("/tasks", {"project_id": project["id"], "prompt": "Control plane validation only; do not call a model", "engine": "forge"})
        registration = {"executor_id": "forge-control-" + uuid4().hex[:8], "engine": "forge", "capacity": 1}
        wrong = await post("/executors/claim", {**registration, "engine": "codex"})
        assert wrong["task"] is None
        lease = (await post("/executors/claim", registration))["task"]
        assert lease["id"] == task["id"]
        prefix, token = "/tasks/" + task["id"], lease["lease_token"]
        source = await client.get(prefix + "/source", headers={"X-Lease-Token": token})
        source.raise_for_status()
        report["engine_routing"] = True
        checkpoint = {"lease_token": token, "expected_revision": 0, "state": {"version": 1, "pending": {"call_id": "edit"}}}
        first = await client.put(prefix + "/checkpoint", json=checkpoint)
        assert first.status_code == 200 and first.json()["revision"] == 1
        assert (await client.put(prefix + "/checkpoint", json=checkpoint)).status_code == 409
        report["checkpoint_cas"] = True
        edit = {"lease_token": token, "call_id": "edit", "name": "replace_text", "arguments": {"path": "app.py", "old_text": "41", "new_text": "42"}}
        result = await post(prefix + "/tools", edit)
        assert not result["cached"] and result["result"]["replacements"] == 1
        replay = await post(prefix + "/tools", edit)
        assert replay["cached"] and replay["result"] == result["result"]
        mismatch = await client.post(prefix + "/tools", json={**edit, "arguments": {**edit["arguments"], "new_text": "43"}})
        assert mismatch.status_code == 409
        report["mutating_call_deduplication"] = report["call_id_mismatch_rejected"] = True
        failed = await post(prefix + "/complete", {"lease_token": token, "exit_code": 1, "error": "Injected worker crash after committed edit"})
        assert failed["status"] == "failed"
        assert (await post(prefix + "/resume"))["status"] == "queued"
        resumed = (await post("/executors/claim", registration))["task"]
        assert resumed["id"] == task["id"] and resumed["lease_token"] != token
        assert (await client.post(prefix + "/tools", json=edit)).status_code == 409
        token = resumed["lease_token"]
        assert (await post(prefix + "/tools", {**edit, "lease_token": token}))["cached"]
        report["resume_preserves_workspace_and_results"] = report["stale_lease_fenced"] = True
        foreign = await client.get(prefix + "/tool-operations", headers={"Authorization": "Bearer " + keys["local-client"]})
        assert foreign.status_code == 404
        report["owner_isolation"] = True
        command = {"lease_token": token, "call_id": "lost-response", "name": "run_command", "arguments": {
            "command": 'python -c "import time; from pathlib import Path; p=Path(\'count.txt\'); p.write_text(str(int(p.read_text())+1) if p.exists() else \'1\'); time.sleep(2)"'}}
        try:
            await client.post(prefix + "/tools", json=command, timeout=0.2)
        except httpx.TimeoutException:
            pass
        # Containers share a bounded semaphore. Wait for the recorded operation,
        # rather than assuming a two-second command got scheduled immediately.
        for _ in range(60):
            await asyncio.sleep(1)
            operations = (await client.get(prefix + "/tool-operations")).json()["operations"]
            matching = [operation for operation in operations if operation["call_id"] == "lost-response"]
            if matching and matching[0]["status"] != "running":
                break
        retried = await client.post(prefix + "/tools", json=command)
        if retried.status_code == 409:
            report["lost_response"] = "uncertain_refused_replay"
            await post(prefix + "/complete", {"lease_token": token, "exit_code": 1, "error": "Uncertain injected tool interruption"})
            assert (await client.post(prefix + "/resume", json={})).status_code == 409
        else:
            retried.raise_for_status()
            assert retried.json()["cached"]
            count = await post(prefix + "/tools", {"lease_token": token, "call_id": "count", "name": "read_file", "arguments": {"path": "count.txt"}})
            assert count["result"]["content"] == "1: 1"
            report["lost_response"] = "committed_result_reused_counter_is_one"
            completed = await post(prefix + "/complete", {"lease_token": token, "exit_code": 0, "summary": "Controlled Forge recovery verification"})
            assert completed["status"] == "awaiting_review" and completed["verification"]["exit_code"] == 0
            await post(prefix + "/reject")
        report["task_id"] = task["id"]
    (ROOT / "results").mkdir(exist_ok=True)
    (ROOT / "results/forge-control.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    asyncio.run(main())

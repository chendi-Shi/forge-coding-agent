"""Inject a lost acknowledgement into the real loop/API/Docker boundary.

Scripted decisions isolate recovery correctness from model quality. This test
spends zero model calls and does not claim to be an Agent intelligence test.
"""

import asyncio
import copy
import json
from pathlib import Path
from uuid import uuid4

import httpx
from dotenv import dotenv_values

from agent_backend.forge.context import MemoryArtifactStore
from agent_backend.forge.providers import ScriptedProvider
from agent_backend.forge.runtime import AgentRuntime

ROOT = Path(__file__).resolve().parents[1]


async def main():
    key = json.loads(dotenv_values(ROOT / ".env")["API_KEYS_JSON"])["isolation-test"]
    async with httpx.AsyncClient(base_url="http://127.0.0.1:2026/api/coding", headers={"Authorization": "Bearer " + key}, timeout=300) as client:
        async def post(path, body=None):
            response = await client.post(path, json=body or {})
            response.raise_for_status()
            return response.json()

        project = await post("/projects", {"name": "Forge loop crash injection", "files": {"app.py": "answer = 1\n"}, "test_command": 'python -c "from app import answer; assert answer == 2"'})
        task = await post("/tasks", {"project_id": project["id"], "engine": "forge", "prompt": "Set the answer to 2 and run the fixed verification"})
        registration = {"executor_id": "recovery-" + uuid4().hex[:8], "engine": "forge", "capacity": 1}
        lease = (await post("/executors/claim", registration))["task"]
        assert lease["id"] == task["id"]
        token, prefix, revision, crashed, cached = lease["lease_token"], "/tasks/" + task["id"], 0, False, []
        (await client.get(prefix + "/source", headers={"X-Lease-Token": token})).raise_for_status()

        async def checkpoint(state):
            nonlocal revision
            response = await client.put(prefix + "/checkpoint", json={"lease_token": token, "expected_revision": revision, "state": state})
            response.raise_for_status()
            revision = response.json()["revision"]

        async def execute(call_id, name, arguments):
            nonlocal crashed
            result = await post(prefix + "/tools", {"lease_token": token, "call_id": call_id, "name": name, "arguments": arguments})
            cached.append(result["cached"])
            if not crashed:
                crashed = True
                raise ConnectionError("Injected lost acknowledgement after committed edit")
            return result["result"]

        async def emit(event):
            pass

        def runtime(decisions):
            return AgentRuntime(provider=ScriptedProvider(decisions), execute=execute, checkpoint=checkpoint, emit=emit, artifacts=MemoryArtifactStore())

        try:
            await runtime([{"action": "tool", "tool": "replace_text", "arguments": {"path": "app.py", "old_text": "1", "new_text": "2"}, "summary": "Edit answer"}]).run(task)
            raise AssertionError("fault was not injected")
        except ConnectionError:
            pass
        saved = (await client.get(prefix + "/checkpoint", headers={"X-Lease-Token": token})).json()
        assert saved["state"]["pending"]["call_id"] == "step-1"
        await post(prefix + "/complete", {"lease_token": token, "exit_code": 1, "error": "Injected lost acknowledgement"})
        await post(prefix + "/resume")
        lease = (await post("/executors/claim", registration))["task"]
        token = lease["lease_token"]
        result = await runtime([{"action": "finish", "tool": "", "arguments": {}, "summary": "Recovered without replaying mutation"}]).run(task, copy.deepcopy(saved["state"]))
        assert result["status"] == "completed" and result["step"] == 2
        assert cached == [False, True, False]
        complete = await post(prefix + "/complete", {"lease_token": token, "exit_code": 0, "summary": result["summary"]})
        assert complete["verification"]["exit_code"] == 0
        operations = (await client.get(prefix + "/tool-operations")).json()["operations"]
        assert len(operations) == 2
        await post(prefix + "/reject")
        report = {"passed": True, "task_id": task["id"], "real_model_calls": 0, "scripted_decisions": 2,
                  "fault": "lost_ack_after_committed_edit_before_observation_checkpoint", "cached_calls": cached,
                  "durable_tool_operations": len(operations), "verification_exit_code": complete["verification"]["exit_code"]}
    (ROOT / "results/forge-recovery.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    asyncio.run(main())

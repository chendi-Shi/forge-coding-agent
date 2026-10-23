"""Exercise real PostgreSQL admission/claims without spending 100 model turns.

Uses the dedicated isolation-test identity, never the active coding worker's
queue. Run against the Compose stack after initializing that optional key.
"""

import asyncio
import json
import math
import os
import time
from pathlib import Path
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]


async def main():
    env = dict(line.split("=", 1) for line in (ROOT / ".env").read_text().splitlines() if "=" in line and not line.startswith("#"))
    keys = json.loads(env["API_KEYS_JSON"])
    if "isolation-test" not in keys:
        raise SystemExit("Add a separate isolation-test identity to API_KEYS_JSON and restart API first.")
    base = os.environ.get("BASE_URL", "http://127.0.0.1:2026") + "/api/coding"
    async with httpx.AsyncClient(base_url=base, headers={"Authorization": f"Bearer {keys['isolation-test']}"}, timeout=180, trust_env=False, limits=httpx.Limits(max_connections=110)) as client:
        project_response = await client.post("/projects", json={"name": "Queue integration check", "files": {"README.md": "Queue validation only. No model execution."}})
        project_response.raise_for_status()
        project = project_response.json()
        bodies = [{"project_id": project["id"], "prompt": f"Queue validation task {index}: do not execute a model", "client_request_id": str(uuid4())} for index in range(101)]
        started = time.perf_counter()
        results = await asyncio.gather(*(client.post("/tasks", json=body) for body in bodies))
        admission_seconds = time.perf_counter() - started
        accepted = [result.json() for result in results if result.status_code == 201]
        assert len(accepted) == 100, [(r.status_code, r.text[:100]) for r in results if r.status_code != 201]
        assert sum(result.status_code == 429 for result in results) == 1
        accepted_ids = {task["id"] for task in accepted}
        first_body = next(body for body in bodies if body["client_request_id"] == accepted[0]["client_request_id"])
        replay = await client.post("/tasks", json=first_body)
        assert replay.status_code == 201 and replay.json()["id"] == accepted[0]["id"]
        forbidden = await client.get(f"/tasks/{accepted[0]['id']}", headers={"Authorization": f"Bearer {keys['local-client']}"})
        assert forbidden.status_code == 404
        started = time.perf_counter()
        claims = await asyncio.gather(*(client.post("/executors/claim", json={"executor_id": f"queue-test-{index % 20}", "capacity": 5, "engine": "forge"}) for index in range(100)))
        claim_seconds = time.perf_counter() - started
        assert all(r.status_code == 200 for r in claims), [(r.status_code, r.text[:100]) for r in claims if r.status_code != 200]
        claimed = [r.json()["task"] for r in claims]
        assert all(claimed) and {t["id"] for t in claimed} == accepted_ids
        overflow = await client.post("/executors/claim", json={"executor_id": "queue-test-0", "capacity": 5, "engine": "forge"})
        assert overflow.json()["task"] is None
        task = claimed[0]
        bad = await client.post(f"/tasks/{task['id']}/events", json={"lease_token": "invalid", "events": []})
        assert bad.status_code == 409
        event = {"lease_token": task["lease_token"], "events": [{"sequence": 0, "event": {"type": "queue.test", "message": "idempotency"}}]}
        for _ in range(2):
            response = await client.post(f"/tasks/{task['id']}/events", json=event)
            response.raise_for_status()
        history = (await client.get(f"/tasks/{task['id']}/events")).json()["events"]
        assert sum(e["kind"] == "queue.test" for e in history) == 1
        async def finish(item):
            cancelled = await client.post(f"/tasks/{item['id']}/cancel")
            cancelled.raise_for_status()
            response = await client.post(f"/tasks/{item['id']}/complete", json={"lease_token": item["lease_token"], "exit_code": 130, "summary": "Queue test; no model launched."})
            response.raise_for_status()
            assert response.json()["status"] == "cancelled"
        await asyncio.gather(*(finish(task) for task in claimed))
        report = {"submitted_concurrently": 101, "admitted": 100, "rejected_at_limit": 1, "unique_leases": len(claimed), "owners_isolated": True, "idempotency": True, "all_cancelled": True, "real_model_calls": 0}
        def latency(responses):
            values = sorted(response.elapsed.total_seconds() * 1000 for response in responses)
            return {label: round(values[math.ceil(len(values) * percentile) - 1], 2)
                    for label, percentile in (("p50_ms", .50), ("p95_ms", .95), ("max_ms", 1))}
        report.update(admission_wall_seconds=round(admission_seconds, 3), claim_wall_seconds=round(claim_seconds, 3),
                      admission_latency=latency(results), claim_latency=latency(claims),
                      measurement="single localhost burst; HTTP client elapsed includes queueing and network; not sustained QPS")
        (ROOT / "results").mkdir(exist_ok=True)
        (ROOT / "results" / "coding-queue.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    asyncio.run(main())

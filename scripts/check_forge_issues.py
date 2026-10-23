"""Submit frozen upstream bugs to the actual Forge worker and save every result.

Reuses saved IDs on rerun. Hidden-oracle grading is a separate evaluator so no
reference implementation or hidden tests enter the model's task workspace.
"""

import asyncio
import base64
import json
import time
from pathlib import Path

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
CASES = {"cachetools-autospec": "python -m pytest -q -o pythonpath=src tests/test_cachedmethod.py",
         "boltons-pearson": "python -m pytest -q tests/test_statsutils.py"}


async def main():
    output = ROOT / "results"
    output.mkdir(exist_ok=True)
    active_file = output / "forge-issues-active.json"
    active = json.loads(active_file.read_text()) if active_file.exists() else {}
    token = json.loads(dotenv_values(ROOT / ".env")["API_KEYS_JSON"])["local-client"]
    async with httpx.AsyncClient(base_url="http://127.0.0.1:2026/api/coding", headers={"Authorization": "Bearer " + token}, timeout=300, trust_env=False) as client:
        async def post(path, body):
            response = await client.post(path, json=body)
            response.raise_for_status()
            return response.json()

        for name, command in CASES.items():
            if name in active:
                continue
            case = ROOT / "benchmarks" / name
            project = await post("/projects", {"name": "OSS · " + name, "archive_base64": base64.b64encode((case / "project.zip").read_bytes()).decode(), "test_command": command})
            task = await post("/tasks", {"project_id": project["id"], "engine": "forge", "prompt": (case / "task.md").read_text(encoding="utf-8"), "timeout_seconds": 1200})
            active[name] = {"task_id": task["id"], "project_id": project["id"]}
            active_file.write_text(json.dumps(active, indent=2), encoding="utf-8")
            print(json.dumps({"submitted": name, "task_id": task["id"]}), flush=True)
        deadline, previous = time.monotonic() + 1300, None
        while time.monotonic() < deadline:
            tasks = {}
            for name, ids in active.items():
                response = await client.get("/tasks/" + ids["task_id"])
                response.raise_for_status()
                tasks[name] = response.json()
            statuses = {name: task["status"] for name, task in tasks.items()}
            if statuses != previous:
                print(json.dumps({"states": statuses}), flush=True)
                previous = statuses
            if all(status not in {"queued", "running", "verifying"} for status in statuses.values()):
                break
            await asyncio.sleep(5)
        reports = {}
        for name, task in tasks.items():
            task_id = task["id"]
            archive = await client.get(f"/tasks/{task_id}/archive")
            archive.raise_for_status()
            (output / f"forge-{name}.zip").write_bytes(archive.content)
            (output / f"forge-{name}.patch").write_text(task["diff"], encoding="utf-8")
            events, cursor = [], 0
            while True:
                batch = (await client.get(f"/tasks/{task_id}/events", params={"after": cursor})).json()["events"]
                events.extend(batch)
                if len(batch) < 200:
                    break
                cursor = batch[-1]["id"]
            (output / f"forge-{name}-events.json").write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8")
            reports[name] = {"task_id": task_id, "status": task["status"], "error": task["error"], "engine": task["engine"],
                             "usage": task["usage"], "started_at": task["started_at"], "finished_at": task["finished_at"],
                             "baseline": task["baseline_test"], "verification": task["verification"], "summary": task["summary"],
                             "checkpoint_revision": task["checkpoint_revision"], "events": len(events), "hidden_oracle": "separate evaluation required"}
        (output / "forge-issues.json").write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({name: {"status": row["status"], "error": row["error"], "usage": row["usage"]} for name, row in reports.items()}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())

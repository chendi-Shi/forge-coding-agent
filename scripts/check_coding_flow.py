"""Live control-plane/artifact verification with deterministic worker uploads.

This does not claim to test a model; real Codex execution is checked separately.
"""

import asyncio
import base64
import io
import json
import zipfile
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def archive(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target:
        for name, content in files.items():
            target.writestr(name, content)
    return base64.b64encode(output.getvalue()).decode()


async def main():
    env = dict(line.split("=", 1) for line in (ROOT / ".env").read_text().splitlines() if "=" in line and not line.startswith("#"))
    key = json.loads(env["API_KEYS_JSON"])["isolation-test"]
    async with httpx.AsyncClient(base_url="http://127.0.0.1:2026/api/coding", headers={"Authorization": f"Bearer {key}"}, timeout=300, trust_env=False) as client:
        async def post(path, body=None):
            result = await client.post(path, json=body or {})
            result.raise_for_status()
            return result.json()

        project = await post("/projects", {"name": "Artifact review integration", "files": {"app.py": "answer = 41\n"}, "test_command": "python -c \"from pathlib import Path; Path('verification-marker.txt').write_text('side effect'); from app import answer; assert answer == 42\""})

        async def start(parent=None):
            body = {"project_id": project["id"], "prompt": "Controlled artifact integration test", "engine": "codex"}
            if parent:
                body["parent_task_id"] = parent
            task = await post("/tasks", body)
            lease = (await post("/executors/claim", {"executor_id": "artifact-check", "capacity": 2}))["task"]
            assert lease and task["id"] == lease["id"]
            source = await client.get(f"/tasks/{task['id']}/source", headers={"X-Lease-Token": lease["lease_token"]})
            source.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(source.content)) as content:
                assert "verification-marker.txt" not in content.namelist()
                files = {name: content.read(name) for name in content.namelist()}
            return task, lease, files

        first, lease, files = await start()
        baseline = (await client.get(f"/tasks/{first['id']}")).json()["baseline_test"]
        assert baseline["exit_code"] != 0
        files["app.py"] = b"answer = 42\n"
        body = {"lease_token": lease["lease_token"], "exit_code": 0, "archive_base64": archive(files), "summary": "Controlled upload"}
        completed = await post(f"/tasks/{first['id']}/complete", body)
        assert completed["status"] == "awaiting_review" and completed["verification"]["exit_code"] == 0
        replay = await post(f"/tasks/{first['id']}/complete", body)
        assert replay["status"] == "awaiting_review"
        assert "+answer = 42" in completed["diff"] and "verification-marker" not in completed["diff"]
        second, second_lease, files = await start(first["id"])
        assert files["app.py"] == b"answer = 42\n"
        files["README.md"] = b"Follow-up artifact\n"
        followup = await post(f"/tasks/{second['id']}/complete", {"lease_token": second_lease["lease_token"], "exit_code": 0, "archive_base64": archive(files)})
        assert followup["verification"]["exit_code"] == 0 and "+answer = 42" in followup["diff"] and "Follow-up artifact" in followup["diff"]
        accepted = await post(f"/tasks/{second['id']}/accept")
        assert accepted["status"] == "accepted"
        late_source = await client.get(f"/tasks/{second['id']}/source", headers={"X-Lease-Token": second_lease["lease_token"]})
        assert late_source.status_code == 409
        stale = await client.post(f"/tasks/{first['id']}/accept")
        assert stale.status_code == 409
        snapshot = await client.get(f"/tasks/{second['id']}/archive")
        snapshot.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(snapshot.content)) as zipped:
            assert "verification-marker.txt" not in zipped.namelist()
            assert zipped.read("app.py") == b"answer = 42\n"
        current = (await client.get("/projects")).json()["projects"]
        assert next(p for p in current if p["id"] == project["id"])["version"] == 2
        stream = await client.get(f"/tasks/{second['id']}/stream")
        assert "task.accepted" in stream.text and "event: end" in stream.text
        invalid, invalid_lease, _ = await start()
        failed = await post(f"/tasks/{invalid['id']}/complete", {"lease_token": invalid_lease["lease_token"], "exit_code": 0, "archive_base64": archive({"../outside.py": "bad"})})
        assert failed["status"] == "failed" and failed["verification"] is None
        report = {"baseline_failed": True, "independent_verification_passed": True, "test_side_effects_isolated": True, "followup_preserves_full_diff": True, "accept_advances_version": True, "stale_accept_409": True, "late_source_409": True, "completion_idempotent": True, "sse_replay": True, "unsafe_archive_failed": True, "real_model_calls": 0}
        (ROOT / "results").mkdir(exist_ok=True)
        (ROOT / "results" / "coding-flow.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    asyncio.run(main())

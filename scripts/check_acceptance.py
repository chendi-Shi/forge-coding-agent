"""Exercise deployed acceptance through real HTTP, PostgreSQL and Docker; no model calls.

Uses only new projects owned by the dedicated isolation-test identity. Run after
deployment and separately from other isolation-test queue probes. Retains task IDs
and failed checks, but never writes bearer tokens, lease tokens, or private tests
to the report. This is a bounded bypass regression, not proof against arbitrary
hostile Python sharing the pytest interpreter.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import os
import shlex
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
BASE_FILES = {"app.py": "answer = 41\n", "tests/test_public.py": "def test_public():\n    assert True\n"}
COUNTERS = ("collected", "passed", "failed", "errors", "skipped", "xfailed", "xpassed")
PLANNED_CASES = [
    "no_suite_accept_refused", "private_suite_and_command_override", "protected_test_modified_by_shell",
    "protected_test_deleted_by_shell", "pytest_ini_bypass", "conftest_bypass", "pytest_module_bypass",
    "sitecustomize_bypass", "fake_pass_stdout", "skipped_suite_refused", "zero_tests_refused",
    "updated_policy_refuses_old_task", "correct_candidate_accepted",
]


class ProbeFailure(Exception):
    pass


def ensure(value, code):
    if not value:
        raise ProbeFailure(code)


def isolated_token() -> str:
    values = dotenv_values(ROOT / ".env")
    try:
        keys = json.loads(os.environ.get("API_KEYS_JSON") or values["API_KEYS_JSON"])
        token = keys["isolation-test"]
        ensure(isinstance(token, str) and len(token) >= 24, "INVALID_ISOLATION_TOKEN")
        return token
    except (KeyError, TypeError, ValueError):
        raise ProbeFailure("ISOLATION_IDENTITY_NOT_CONFIGURED") from None


def suite_spec(marker: str, *, code: str | None = None) -> dict:
    source = code or "from app import answer\n\ndef test_private_answer():\n    assert answer == 42\n"
    return {
        "name": "Private regression policy", "files": {"test_private_acceptance.py": f"# {marker}\n" + source},
        "test_paths": ["test_private_acceptance.py"], "python_paths": ["."],
        "protected_paths": ["tests/test_public.py"], "minimum_tests": 1, "timeout_seconds": 30,
    }


class Harness:
    def __init__(self, client, output: Path):
        self.client, self.output = client, output
        self.executor = "acceptance-probe-" + uuid4().hex[:12]
        self.marker = "PRIVATE_ACCEPTANCE_SENTINEL_" + uuid4().hex
        self.private_paths = {"test_private_acceptance.py"}
        self.heartbeat = None
        self.current = None
        self.report = {
            "started_at": datetime.now(UTC).isoformat(), "real_model_calls": 0,
            "scope": "Deployed HTTP APIs backed by PostgreSQL and real Docker verification; scripted Forge tools, no model.",
            "limitations": ["Not a proof against arbitrary malicious Python in the pytest process.",
                            "No direct PostgreSQL fault injection and no service/worker termination.",
                            "Only newly created isolation-test projects are mutated."],
            "cases": [], "all_passed": False,
        }

    def save(self):
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.output.write_text(json.dumps(self.report, ensure_ascii=False, indent=2), encoding="utf-8")

    async def request(self, method, path, body=None, *, expected=(200, 201), **kwargs):
        if body is not None:
            kwargs["json"] = body
        response = await self.client.request(method, path, **kwargs)
        if response.status_code not in expected:
            raise ProbeFailure(f"HTTP_{response.status_code}_{method}_{path}")
        return response

    async def post(self, path, body=None):
        return (await self.request("POST", path, body or {})).json()

    async def preflight(self):
        tasks = (await self.request("GET", "/tasks")).json()["tasks"]
        ensure(not any(task["engine"] == "forge" and task["status"] in {"queued", "running", "verifying"} for task in tasks),
               "EXISTING_ISOLATION_FORGE_TASKS_REQUIRE_SERIAL_RUN")
        status = (await self.request("GET", "/status")).json()
        ensure(not any(status["counts"].get(state, 0) for state in ("queued", "running", "verifying")),
               "EXISTING_ISOLATION_TASKS_REQUIRE_SERIAL_RUN")
        ensure(not any(executor.get("engine") == "forge" for executor in status["executors"]),
               "ANOTHER_ISOLATION_FORGE_EXECUTOR_IS_ONLINE")

    async def project(self, *, policy=True, code=None):
        project = await self.post("/projects", {
            "name": "Acceptance probe " + uuid4().hex[:8], "files": BASE_FILES, "test_command": "true",
        })
        accepted = None
        if policy:
            accepted = await self.post(f"/projects/{project['id']}/acceptance-suites", suite_spec(self.marker, code=code))
            self.check_private(accepted)
            metadata = (await self.request("GET", f"/projects/{project['id']}/acceptance-suites")).json()
            self.check_private(metadata)
            ensure(metadata["active_suite_id"] == accepted["id"], "SUITE_NOT_PINNED_TO_PROJECT")
        return project, accepted

    def check_private(self, value):
        serialized = json.dumps(value, ensure_ascii=False)
        ensure(self.marker not in serialized and not any(path in serialized for path in self.private_paths), "PRIVATE_SUITE_LEAKED_IN_API")

    async def _heartbeat(self, task_id, lease_token):
        while True:
            await asyncio.sleep(5)
            response = await self.client.post("/executors/heartbeat", json={
                "executor_id": self.executor, "task_id": task_id, "lease_token": lease_token,
            }, timeout=20)
            if response.status_code == 409:
                return
            if response.status_code != 200:
                raise ProbeFailure("HEARTBEAT_FAILED")

    async def start(self, project, case):
        task = await self.post("/tasks", {
            "project_id": project["id"], "engine": "forge", "test_command": "true", "timeout_seconds": 600,
            "prompt": "Deterministic acceptance policy probe; no model invocation is authorized by this script.",
        })
        case.update(project_id=project["id"], task_id=task["id"], suite_id=task.get("acceptance_suite_id"))
        self.save()
        lease = (await self.post("/executors/claim", {
            "executor_id": self.executor, "engine": "forge", "capacity": 1, "version": "acceptance-probe-v1",
        }))["task"]
        ensure(lease and lease["id"] == task["id"], "CLAIM_RETURNED_DIFFERENT_TASK")
        self.check_private(lease)
        self.current = lease
        self.heartbeat = asyncio.create_task(self._heartbeat(task["id"], lease["lease_token"]))
        source = await self.request("GET", f"/tasks/{task['id']}/source", headers={"X-Lease-Token": lease["lease_token"]})
        with zipfile.ZipFile(io.BytesIO(source.content)) as content:
            ensure(not self.private_paths.intersection(content.namelist()), "PRIVATE_SUITE_PRESENT_IN_SOURCE")
            ensure(not any(self.marker.encode() in content.read(name) for name in content.namelist() if not name.endswith("/")),
                   "PRIVATE_MARKER_PRESENT_IN_SOURCE")
        self.check_private((await self.request("GET", f"/tasks/{task['id']}")).json())
        return lease

    async def tool(self, lease, name, arguments):
        response = await self.post(f"/tasks/{lease['id']}/tools", {
            "lease_token": lease["lease_token"], "call_id": "probe-" + uuid4().hex,
            "name": name, "arguments": arguments,
        })
        return response["result"]

    async def write(self, lease, path, content):
        result = await self.tool(lease, "write_file", {"path": path, "content": content})
        ensure(not result.get("error"), "WRITE_TOOL_FAILED")

    async def shell(self, lease, python_code):
        result = await self.tool(lease, "run_command", {"command": "python -c " + shlex.quote(python_code)})
        ensure(result.get("exit_code") == 0, "CONTROLLED_SHELL_OPERATION_FAILED")
        return result

    async def finish(self, lease, case, *, expected_pass=False):
        result = await self.tool(lease, "run_tests", {})
        self.check_private(result)
        accepted = result.get("acceptance", {})
        ensure(accepted.get("passed") is expected_pass, "RUN_TESTS_DID_NOT_ENFORCE_SUITE")
        completed = await self.post(f"/tasks/{lease['id']}/complete", {
            "lease_token": lease["lease_token"], "exit_code": 0, "summary": "Scripted acceptance probe; no model calls.",
        })
        await self.stop_heartbeat()
        self.current = None
        self.check_private(completed)
        verification = completed.get("verification") or {}
        final = verification.get("acceptance", {})
        ensure(final.get("passed") is expected_pass, "COMPLETE_DID_NOT_ENFORCE_SUITE")
        ensure((verification.get("exit_code") == 0) is expected_pass, "VERIFICATION_EXIT_CODE_MISMATCH")
        events = (await self.request("GET", f"/tasks/{lease['id']}/events")).json()
        self.check_private(events)
        case.update(status=completed["status"], verification=verification)
        return completed

    async def refused(self, task_id, case):
        response = await self.request("POST", f"/tasks/{task_id}/accept", {}, expected=(409,))
        case["accept_status"] = response.status_code

    async def stop_heartbeat(self):
        if self.heartbeat:
            self.heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError, httpx.HTTPError, ProbeFailure):
                await self.heartbeat
            self.heartbeat = None

    async def cleanup_failed_case(self):
        if self.current:
            with contextlib.suppress(httpx.HTTPError, ProbeFailure):
                await self.post(f"/tasks/{self.current['id']}/complete", {
                    "lease_token": self.current["lease_token"], "exit_code": 1,
                    "error": "Acceptance probe stopped after a failed assertion; no model was called.",
                })
        await self.stop_heartbeat()
        self.current = None

    async def case(self, name, action):
        case = {"name": name, "passed": False}
        self.report["cases"].append(case)
        started = time.monotonic()
        try:
            await action(case)
            case["passed"] = True
        except (ProbeFailure, httpx.HTTPError, KeyError, ValueError) as error:
            case["error"] = str(error) if isinstance(error, ProbeFailure) else type(error).__name__
            await self.cleanup_failed_case()
        case["seconds"] = round(time.monotonic() - started, 3)
        self.save()
        print(json.dumps({"case": name, "passed": case["passed"], "task_id": case.get("task_id"), "error": case.get("error")}), flush=True)

    async def run(self):
        await self.preflight()

        async def no_suite(case):
            project, _ = await self.project(policy=False)
            lease = await self.start(project, case)
            completed = await self.post(f"/tasks/{lease['id']}/complete", {"lease_token": lease["lease_token"], "exit_code": 0})
            self.current = None
            await self.stop_heartbeat()
            ensure(completed["status"] == "awaiting_review", "NO_SUITE_TASK_NOT_REVIEWABLE")
            await self.refused(lease["id"], case)

        await self.case("no_suite_accept_refused", no_suite)
        project, _ = await self.project()

        async def bad_candidate(case):
            lease = await self.start(project, case)
            await self.finish(lease, case)
            await self.refused(lease["id"], case)
            case["private_source_and_metadata"] = "absent"
            case["development_command_override"] = "true did not bypass private tests"

        await self.case("private_suite_and_command_override", bad_candidate)

        for name, code in [
            ("protected_test_modified_by_shell", "from pathlib import Path; Path('tests/test_public.py').write_text('# disabled\\n')"),
            ("protected_test_deleted_by_shell", "from pathlib import Path; Path('tests/test_public.py').unlink()"),
        ]:
            async def protected(case, code=code):
                lease = await self.start(project, case)
                await self.write(lease, "app.py", "answer = 42\n")
                await self.shell(lease, code)
                await self.finish(lease, case)
                ensure(case["verification"]["acceptance"]["violations"], "PROTECTED_MUTATION_NOT_REPORTED")
                await self.refused(lease["id"], case)
            await self.case(name, protected)

        bypasses = [
            ("pytest_ini_bypass", "pytest.ini", "[pytest]\naddopts = -k nonexistent_test\n"),
            ("conftest_bypass", "conftest.py", "def pytest_collection_modifyitems(items):\n    items.clear()\n"),
            ("pytest_module_bypass", "pytest.py", "print('999 passed'); raise SystemExit(0)\n"),
            ("sitecustomize_bypass", "sitecustomize.py", "import os\nprint('999 passed', flush=True)\nos._exit(0)\n"),
        ]
        for name, path, code in bypasses:
            async def bypass(case, path=path, code=code):
                lease = await self.start(project, case)
                await self.write(lease, path, code)
                await self.finish(lease, case)
                tests = case["verification"]["acceptance"]["tests"]
                ensure(tests.get("collected") == 1 and tests.get("failed") == 1, "CANDIDATE_CONFIGURATION_CHANGED_COLLECTION")
                await self.refused(lease["id"], case)
            await self.case(name, bypass)

        async def fake_print(case):
            lease = await self.start(project, case)
            result = await self.shell(lease, "print('999 passed in 0.01s')")
            ensure("999 passed" in result.get("stdout", ""), "FAKE_PASS_PRINT_NOT_EXECUTED")
            await self.finish(lease, case)
            await self.refused(lease["id"], case)
        await self.case("fake_pass_stdout", fake_print)

        for name, code in [
            ("skipped_suite_refused", "import pytest\n@pytest.mark.skip(reason='controlled skip')\ndef test_skipped():\n    assert False\n"),
            ("zero_tests_refused", "# Deliberately defines no tests.\nvalue = 1\n"),
        ]:
            async def incomplete(case, code=code):
                special, _ = await self.project(code=code)
                lease = await self.start(special, case)
                await self.write(lease, "app.py", "answer = 42\n")
                await self.finish(lease, case)
                await self.refused(lease["id"], case)
            await self.case(name, incomplete)

        async def stale_policy(case):
            lease = await self.start(project, case)
            await self.write(lease, "app.py", "answer = 42\n")
            await self.finish(lease, case, expected_pass=True)
            updated = await self.post(f"/projects/{project['id']}/acceptance-suites", suite_spec(self.marker))
            case["new_suite_id"] = updated["id"]
            ensure(updated["id"] != case["suite_id"], "POLICY_VERSION_NOT_ADVANCED")
            await self.refused(lease["id"], case)
        await self.case("updated_policy_refuses_old_task", stale_policy)

        async def valid(case):
            lease = await self.start(project, case)
            await self.write(lease, "app.py", "answer = 42\n")
            await self.finish(lease, case, expected_pass=True)
            accepted = await self.post(f"/tasks/{lease['id']}/accept")
            ensure(accepted["status"] == "accepted", "CORRECT_CANDIDATE_NOT_ACCEPTED")
            case["accept_status"] = 200
            source = (await self.request("GET", f"/projects/{project['id']}/file", params={"path": "app.py"})).json()
            ensure(source.get("content") == "answer = 42\n", "ACCEPT_DID_NOT_ADVANCE_PROJECT")
        await self.case("correct_candidate_accepted", valid)

        self.report["finished_at"] = datetime.now(UTC).isoformat()
        self.report["all_passed"] = len(self.report["cases"]) == len(PLANNED_CASES) and all(case["passed"] for case in self.report["cases"])
        self.save()
        return self.report["all_passed"]


async def main(args):
    report_path = await asyncio.to_thread(Path(args.report).resolve)
    if args.dry_run:
        print(json.dumps({"planned_cases": PLANNED_CASES, "real_model_calls": 0, "executes_requests": False}, indent=2))
        return 0
    if report_path.exists():
        previous = report_path.with_name(report_path.stem + "-previous-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:6] + ".json")
        report_path.replace(previous)
    async with httpx.AsyncClient(
        base_url=args.base_url.rstrip("/") + "/api/coding", timeout=300, trust_env=False,
        headers={"Authorization": "Bearer " + isolated_token()},
    ) as client:
        harness = Harness(client, report_path)
        try:
            passed = await harness.run()
        except (ProbeFailure, httpx.HTTPError, KeyError, ValueError) as error:
            harness.report["fatal_error"] = str(error) if isinstance(error, ProbeFailure) else type(error).__name__
            harness.save()
            await harness.cleanup_failed_case()
            passed = False
        print(json.dumps({"all_passed": passed, "checks": len(harness.report["cases"]), "real_model_calls": 0}), flush=True)
        return 0 if passed else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:2026")
    parser.add_argument("--report", default=str(ROOT / "results/acceptance-live.json"))
    parser.add_argument("--dry-run", action="store_true")
    raise SystemExit(asyncio.run(main(parser.parse_args())))

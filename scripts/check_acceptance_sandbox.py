#!/usr/bin/env python3
"""Real Docker acceptance checks using disposable private-broker workspaces.

Run after deploying the sandbox images. No model calls or production task changes.
"""

from __future__ import annotations

import argparse
import inspect
import json
import subprocess
from pathlib import Path

if __package__:
    from .recovery_test import DockerProject
else:
    from recovery_test import DockerProject

ROOT = Path(__file__).resolve().parents[1]


async def remote_probe():
    import hashlib
    import io
    import os
    import time
    import zipfile
    from uuid import uuid4

    import httpx

    from agent_backend.sandbox.engine import OWNER_LABEL, SOURCE_LABEL, WORKSPACE_LABEL, engine_client

    started = time.monotonic()
    report = {"ok": False, "real_model_calls": 0, "checks": [],
              "boundary": "Read-only suite and fixed pytest policy; not protection against malicious Python tampering within the pytest process."}
    created = []
    suite = "from subject import answer\ndef test_answer():\n    assert answer == 42\n"
    readonly = """
def test_suite_is_readonly():
    from pathlib import Path
    import errno
    try:
        Path('/acceptance/forbidden.py').write_text('changed')
    except OSError as error:
        assert error.errno == errno.EROFS
    else:
        raise AssertionError('acceptance volume was writable')
"""
    cases = [
        ("passing_suite_and_readonly_mount", {"subject.py": "answer = 42"}, suite + readonly, True, 10),
        ("delete_hidden_test_is_rejected", {"subject.py": "from pathlib import Path\nPath('/acceptance/test_hidden.py').unlink()\nanswer = 42"}, suite, False, 10),
        ("repository_config_and_conftest_cannot_skip", {"subject.py": "answer = 41", "pytest.ini": "[pytest]\naddopts = -k nonexistent\n", "pytest.py": "raise RuntimeError('fake pytest')", "sitecustomize.py": "raise RuntimeError('fake site')", "conftest.py": "import pytest\ndef pytest_collection_modifyitems(items):\n    for item in items: item.add_marker(pytest.mark.skip)\n"}, suite, False, 10),
        ("fake_stdout_cannot_report_success", {"subject.py": "print('100 passed; {\"passed\":true}')\nanswer = 41"}, suite, False, 10),
        ("zero_tests_fail_closed", {}, "VALUE = 1\n", False, 10),
        ("skipped_tests_fail_closed", {}, "import pytest\n@pytest.mark.skip\ndef test_skip(): pass\n", False, 10),
        ("xfail_tests_fail_closed", {}, "import pytest\n@pytest.mark.xfail\ndef test_xfail(): assert False\n", False, 10),
        ("xpass_tests_fail_closed", {}, "import pytest\n@pytest.mark.xfail\ndef test_xpass(): pass\n", False, 10),
        ("command_timeout_fails_closed", {}, "import time\ndef test_slow(): time.sleep(4)\n", False, 1),
        ("image_import_collision_is_unsupported", {"json.py": "CANDIDATE_ONLY = True"}, "def test_ok(): assert True\n", False, 10),
    ]
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8090", headers={"Authorization": "Bearer " + os.environ["BROKER_TOKEN"]}, timeout=180, trust_env=False) as client:
        async def request(method, path, **kwargs):
            response = await client.request(method, path, **kwargs)
            if response.is_error:
                raise RuntimeError(f"BROKER_HTTP_{response.status_code}")
            return response

        async def digest(identity):
            response = await request("GET", f"/workspaces/{identity}/archive")
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                return {name: hashlib.sha256(archive.read(name)).hexdigest() for name in archive.namelist()}

        try:
            for name, files, source, expected, seconds in cases:
                identity = str(uuid4())
                created.append(identity)
                await request("POST", "/workspaces", json={"workspace_id": identity, "files": [{"path": path, "content": text} for path, text in files.items()]})
                before = await digest(identity)
                result = (await request("POST", f"/workspaces/{identity}/verify", json={
                    "files": {"test_hidden.py": source}, "test_paths": ["test_hidden.py"], "python_paths": ["."],
                    "minimum_tests": 1, "timeout_seconds": seconds,
                })).json()
                preserved = await digest(identity) == before
                passed = (result.get("passed") is expected and preserved
                          and set(result.get("tests", {})) == {"collected", "passed", "failed", "errors", "skipped", "xfailed", "xpassed"})
                if name == "command_timeout_fails_closed":
                    passed &= result.get("timed_out") is True
                if name == "repository_config_and_conftest_cannot_skip":
                    passed &= result.get("tests", {}).get("failed") == 1 and not result.get("unsupported_reason")
                if name == "image_import_collision_is_unsupported":
                    passed &= result.get("unsupported_reason") == "import_collision:json"
                report["checks"].append({"name": name, "passed": passed, "accepted": result.get("passed"),
                                         "tests": result.get("tests"), "candidate_unchanged": preserved,
                                         "exit_code": result.get("exit_code"), "timed_out": result.get("timed_out"),
                                         "unsupported_reason": result.get("unsupported_reason")})
                if not passed:
                    raise RuntimeError("ACCEPTANCE_CHECK_FAILED_" + name)
            report["ok"] = True
        except Exception as error:
            report["error"] = str(error)[:200]
        finally:
            cleanup = []
            for identity in created:
                try:
                    await request("DELETE", f"/workspaces/{identity}")
                except Exception:
                    cleanup.append(identity)
            async with engine_client() as engine:
                volumes = (await engine.get("/volumes")).json().get("Volumes") or []
                leaked = [volume.get("Name") for volume in volumes if (volume.get("Labels") or {}).get(OWNER_LABEL) == "true"
                          and ((volume.get("Labels") or {}).get(SOURCE_LABEL) in created or (volume.get("Labels") or {}).get(WORKSPACE_LABEL) in created)]
            report["cleanup"] = {"source_delete_failures": cleanup, "remaining_source_or_verification_volumes": leaked}
            report["ok"] &= not cleanup and not leaked
    report["passed"] = sum(check["passed"] for check in report["checks"])
    report["duration_seconds"] = round(time.monotonic() - started, 3)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "results" / "acceptance-sandbox.json")
    args = parser.parse_args()
    source = "import asyncio,json\n" + inspect.getsource(remote_probe) + "\nprint(json.dumps(asyncio.run(remote_probe())))\n"
    project = DockerProject(ROOT)
    process = subprocess.run([*project.compose, "exec", "-T", "sandbox-broker", "python", "-"], input=source,
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
    try:
        report = json.loads(process.stdout)
    except ValueError:
        report = {"ok": False, "error": "BROKER_PROBE_DID_NOT_RETURN_JSON", "process_exit_code": process.returncode}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": report.get("ok"), "passed": report.get("passed", 0), "error": report.get("error"), "report": str(args.output)}))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

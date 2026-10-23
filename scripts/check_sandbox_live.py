#!/usr/bin/env python3
"""Exercise the deployed private broker using only disposable probe workspaces.

Compose supplies BROKER_TOKEN from .env to the broker. The probe runs inside that
container, so neither the token nor a public broker port is needed on the caller.
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
    """Self-contained source sent to `compose exec -T sandbox-broker python -`."""
    import asyncio
    import base64
    import hashlib
    import io
    import json
    import os
    import re
    import stat
    import time
    import zipfile
    from datetime import UTC, datetime
    from uuid import uuid4

    import httpx

    from agent_backend.sandbox.engine import OWNER_LABEL, WORKSPACE_LABEL, engine_client, volume_name

    started = time.monotonic()
    report = {
        "schema_version": 1, "started_at": datetime.now(UTC).isoformat(),
        "scope": "Real Docker sandbox CRUD, archives, command execution and resource-policy validation; no LLM throughput claim.",
        "checks": [], "workspace_ids": [], "cleanup": {}, "ok": False,
    }
    identities = [str(uuid4()), str(uuid4())]
    report["workspace_ids"] = identities
    attempted = []
    observed_inputs = set()
    current_check = "initialization"
    token = os.environ.get("BROKER_TOKEN", "")

    class Failure(Exception):
        pass

    def check(name, condition, **details):
        nonlocal current_check
        current_check = name
        report["checks"].append({"name": name, "passed": bool(condition), **details})
        if not condition:
            raise Failure(name)

    def archive_bytes(files):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, content in files.items():
                archive.writestr(name, content)
        return output.getvalue()

    def encoded(files):
        return base64.b64encode(archive_bytes(files)).decode()

    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:8090", headers={"Authorization": "Bearer " + token},
        timeout=180, trust_env=False,
    ) as client, engine_client() as engine:
        async def api(method, path, *, expected=200, raw=False, **kwargs):
            response = await client.request(method, path, **kwargs)
            if response.status_code != expected:
                code = "UNEXPECTED_STATUS"
                try:
                    candidate = response.json().get("error", {}).get("code", "")
                    if isinstance(candidate, str) and re.fullmatch(r"[A-Za-z0-9_]{1,100}", candidate):
                        code = candidate
                except (ValueError, TypeError, AttributeError):
                    pass
                raise Failure(f"{current_check}: HTTP {response.status_code}, expected {expected}, {code}")
            return response.content if raw else response.json()

        async def tool(identity, name, arguments=None, *, expected=200):
            return await api("POST", f"/workspaces/{identity}/tools", expected=expected, json={"name": name, "arguments": arguments or {}})

        async def engine_get(path, **kwargs):
            response = await engine.get(path, **kwargs)
            if response.status_code != 200:
                raise Failure("DOCKER_INSPECTION_FAILED")
            return response.json()

        async def own_containers(identity):
            filters = json.dumps({"label": [OWNER_LABEL + "=true", WORKSPACE_LABEL + "=" + identity]})
            return await engine_get("/containers/json", params={"all": "true", "filters": filters})

        try:
            current_check = "private_authentication_and_health"
            denied = await client.get("/health", headers={"Authorization": "Bearer deliberately-invalid-test-token"})
            health = await api("GET", "/health")
            check(current_check, denied.status_code == 401 and health.get("status") == "ok" and health.get("execution_network") == "none")

            current_check = "create_workspace_and_git_baseline"
            source, forked = identities
            original = "def add(a, b):\n    return a + b\n"
            updated = original + "\n# changed by sandbox live probe\n"
            test_source = "from maths import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"
            files = {"maths.py": original, "test_maths.py": test_source, "old.txt": "remove during restore\n"}
            attempted.append(source)
            created = await api("POST", "/workspaces", expected=201, json={"workspace_id": source, "files": [{"path": path, "content": content} for path, content in files.items()]})
            baseline = created.get("baseline")
            check(current_check, created.get("workspace_id") == source and isinstance(baseline, str) and bool(re.fullmatch(r"[0-9a-f]{40,64}", baseline)))

            current_check = "file_crud_and_text_replacement"
            listed = await tool(source, "list_files")
            initial = await tool(source, "read_file", {"path": "maths.py"})
            await tool(source, "write_file", {"path": "scratch.txt", "content": "alpha beta"})
            replaced = await tool(source, "replace_text", {"path": "scratch.txt", "old_text": "beta", "new_text": "gamma"})
            edited = await tool(source, "read_file", {"path": "scratch.txt"})
            await tool(source, "delete_file", {"path": "scratch.txt"})
            absent = await tool(source, "read_file", {"path": "scratch.txt"}, expected=422)
            check(current_check, set(listed.get("files", [])) == set(files) and initial.get("content") == original and replaced.get("replacements") == 1 and edited.get("content") == "alpha gamma" and absent.get("error", {}).get("code") == "not_found")

            current_check = "modified_source_fork_gets_a_new_baseline"
            await tool(source, "write_file", {"path": "maths.py", "content": updated})
            source_diff = await api("GET", f"/workspaces/{source}/diff")
            attempted.append(forked)
            fork_result = await api("POST", f"/workspaces/{source}/fork", expected=201, json={"workspace_id": forked})
            fork_diff = await api("GET", f"/workspaces/{forked}/diff")
            fork_file = await tool(forked, "read_file", {"path": "maths.py"})
            check(current_check, "changed by sandbox live probe" in source_diff.get("diff", "") and fork_result.get("workspace_id") == forked and fork_diff.get("diff") == "" and fork_file.get("content") == updated)

            current_check = "fork_retries_are_idempotent"
            await tool(forked, "write_file", {"path": "retry-proof.txt", "content": "must survive duplicate fork"})
            repeated = await api("POST", f"/workspaces/{source}/fork", expected=201, json={"workspace_id": forked})
            preserved = await tool(forked, "read_file", {"path": "retry-proof.txt"})
            check(current_check, repeated.get("existing") is True and preserved.get("content") == "must survive duplicate fork")

            current_check = "restore_deletions_binary_diff_and_archive_roundtrip"
            binary = b"\x00\xff\x01\x00sandbox-binary\x00" * 16
            restored_files = {"maths.py": updated, "test_maths.py": test_source, "asset.bin": binary}
            restored = await api("POST", f"/workspaces/{source}/restore", json={"archive_base64": encoded(restored_files)})
            downloaded = await api("GET", f"/workspaces/{source}/archive", raw=True)
            with zipfile.ZipFile(io.BytesIO(downloaded)) as archive:
                exported = {name: archive.read(name) for name in archive.namelist()}
            patch = restored.get("diff", "")
            check(current_check, restored.get("files") == 3 and "GIT binary patch" in patch and "deleted file mode" in patch and "old.txt" in patch and set(exported) == set(restored_files) and exported["asset.bin"] == binary, binary_sha256=hashlib.sha256(binary).hexdigest())

            current_check = "malicious_archives_are_rejected_without_modification"
            invalid_archives = [encoded({"../outside.txt": "bad"}), encoded({".git/HEAD": "bad"}), encoded({".env": "bad"})]
            symlink_buffer = io.BytesIO()
            with zipfile.ZipFile(symlink_buffer, "w") as archive:
                entry = zipfile.ZipInfo("linked-file")
                entry.create_system = 3
                entry.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(entry, "/etc/passwd")
            invalid_archives.append(base64.b64encode(symlink_buffer.getvalue()).decode())
            for bad in invalid_archives:
                rejected = await api("POST", f"/workspaces/{source}/restore", expected=422, json={"archive_base64": bad})
                if "error" not in rejected:
                    raise Failure("INVALID_ARCHIVE_RETURNED_NO_ERROR")
            after = await api("GET", f"/workspaces/{source}/archive", raw=True)
            with zipfile.ZipFile(io.BytesIO(after)) as archive:
                after_files = {name: archive.read(name) for name in archive.namelist()}
            check(current_check, exported == after_files, rejected_cases=len(invalid_archives))

            current_check = "real_python_pytest_and_node_execution"
            tests = await tool(source, "run_command", {"argv": ["python", "-m", "pytest", "-q"], "timeout_seconds": 20})
            node = await tool(source, "run_command", {"argv": ["node", "-e", "console.log(6*7)"], "timeout_seconds": 10})
            check(current_check, tests.get("exit_code") == 0 and "1 passed" in tests.get("stdout", "") and node.get("exit_code") == 0 and node.get("stdout", "").strip() == "42")

            current_check = "runtime_isolation_and_docker_policy"
            command_source = (
                "import os,json,pathlib,time; time.sleep(3); "
                "d={'uid':os.getuid(),'cwd':os.getcwd(),'interfaces':sorted(os.listdir('/sys/class/net')),"
                "'docker_socket':pathlib.Path('/var/run/docker.sock').exists(),"
                "'has_model_or_broker_secret':any(k in os.environ for k in ['OPENAI_API_KEY','BROKER_TOKEN','API_KEYS_JSON'])}; "
                "exec(\"for name,path in [('root_write','/opt/sandbox/live-probe'),('git_write','/workspace/.git/HEAD')]:\\n try: pathlib.Path(path).write_text('probe'); d[name]=0\\n except OSError as e: d[name]=e.errno\"); "
                "print(json.dumps(d))"
            )
            execution = asyncio.create_task(tool(source, "run_command", {"argv": ["python", "-c", command_source], "timeout_seconds": 15}))
            inspected = None
            until = time.monotonic() + 15
            while time.monotonic() < until:
                candidates = await own_containers(source)
                running = [container for container in candidates if container.get("State") == "running"]
                if running:
                    inspected = await engine_get("/containers/" + running[0]["Id"] + "/json")
                    break
                if execution.done():
                    break
                await asyncio.sleep(0.05)
            runtime_result = await execution
            if runtime_result.get("exit_code") != 0 or inspected is None:
                raise Failure("RUNTIME_PROBE_DID_NOT_EXECUTE")
            runtime = json.loads(runtime_result["stdout"])
            host = inspected["HostConfig"]
            mounts = inspected.get("Mounts", [])
            labels = inspected["Config"].get("Labels") or {}
            if labels.get("agent-backend.input-volume"):
                observed_inputs.add(labels["agent-backend.input-volume"])
            baseline_mount = [mount for mount in mounts if mount.get("Destination") == "/workspace/.git"]
            check(current_check,
                runtime.get("uid") == 10001 and runtime.get("cwd") == "/workspace"
                and runtime.get("interfaces") == ["lo"] and not runtime.get("docker_socket") and not runtime.get("has_model_or_broker_secret")
                and runtime.get("root_write") in {13, 30} and runtime.get("git_write") == 30
                and host.get("ReadonlyRootfs") and host.get("NetworkMode") == "none" and not host.get("Privileged")
                and host.get("CapDrop") == ["ALL"] and host.get("PidsLimit") == 128
                and host.get("Memory") == 1024**3 and host.get("MemorySwap") == 1024**3 and host.get("NanoCpus") == 10**9
                and all(mount.get("Type") == "volume" for mount in mounts)
                and len(baseline_mount) == 1 and baseline_mount[0].get("RW") is False,
                uid=runtime.get("uid"), network=host.get("NetworkMode"), memory_bytes=host.get("Memory"), pids=host.get("PidsLimit"), cpu=host.get("NanoCpus", 0) / 10**9,
            )

            current_check = "command_timeout_is_enforced"
            timed = await tool(source, "run_command", {"argv": ["python", "-c", "import time; print('started',flush=True); time.sleep(10)"], "timeout_seconds": 0.5})
            check(current_check, timed.get("timed_out") is True and timed.get("exit_code") != 0 and timed.get("duration", 999) < 5, duration=timed.get("duration"))

            current_check = "command_output_is_bounded"
            output = await tool(source, "run_command", {"argv": ["python", "-c", "print('x'*100000)"], "max_output_bytes": 1024, "timeout_seconds": 10})
            size = len(output.get("stdout", "").encode()) + len(output.get("stderr", "").encode())
            check(current_check, output.get("exit_code") == 0 and output.get("truncated") is True and size == 1024, captured_bytes=size)

            current_check = "archive_excludes_secrets_dependencies_and_metadata"
            seeded = await tool(source, "run_command", {"argv": ["python", "-c", "from pathlib import Path; Path('.env').write_text('synthetic-probe-only'); Path('node_modules').mkdir(exist_ok=True); Path('node_modules/cache.txt').write_text('generated'); Path('.codex').mkdir(exist_ok=True); Path('.codex/cache').write_text('generated')"], "timeout_seconds": 10})
            if seeded.get("exit_code") != 0:
                raise Failure("ARCHIVE_EXCLUSION_SETUP_FAILED")
            clean_archive = await api("GET", f"/workspaces/{source}/archive", raw=True)
            with zipfile.ZipFile(io.BytesIO(clean_archive)) as archive:
                names = archive.namelist()
            check(current_check, set(names) == set(restored_files))

            current_check = "execution_containers_are_removed_after_each_request"
            remaining = [container for identity in identities for container in await own_containers(identity)]
            check(current_check, not remaining, remaining_count=len(remaining))
        except Failure as error:
            report["error"] = str(error)[:500]
            if not report["checks"] or report["checks"][-1]["name"] != current_check or report["checks"][-1]["passed"]:
                report["checks"].append({"name": current_check, "passed": False})
        except Exception as error:
            report["error"] = "PROBE_EXCEPTION_" + type(error).__name__
            report["checks"].append({"name": current_check, "passed": False})
        finally:
            cleanup_errors = []
            for identity in reversed(attempted):
                try:
                    await api("DELETE", f"/workspaces/{identity}")
                except Exception:
                    cleanup_errors.append(identity)
            volumes_remaining = []
            for identity in attempted:
                try:
                    response = await engine.get("/volumes/" + volume_name(identity))
                    missing = response.status_code == 404
                except Exception:
                    missing = False
                if not missing:
                    volumes_remaining.append(identity)
            inputs_remaining = []
            for name in observed_inputs:
                try:
                    response = await engine.get("/volumes/" + name)
                    missing = response.status_code == 404
                except Exception:
                    missing = False
                if not missing:
                    inputs_remaining.append(name)
            report["cleanup"] = {
                "attempted_workspaces": len(attempted), "failed_workspace_ids": cleanup_errors,
                "remaining_workspace_ids": volumes_remaining, "remaining_observed_input_volumes": inputs_remaining,
            }
            report["checks"].append({"name": "only_probe_workspaces_and_observed_inputs_are_cleaned", "passed": not cleanup_errors and not volumes_remaining and not inputs_remaining})
    report["duration_seconds"] = round(time.monotonic() - started, 3)
    report["passed"] = sum(item["passed"] for item in report["checks"])
    report["failed"] = len(report["checks"]) - report["passed"]
    report["ok"] = not report["failed"] and "error" not in report
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "results" / "sandbox-live.json")
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    if not 60 <= args.timeout <= 1800:
        parser.error("--timeout must be between 60 and 1800 seconds")
    source = "import asyncio,json,sys\n" + inspect.getsource(remote_probe)
    source += "\nreport=asyncio.run(remote_probe())\nprint(json.dumps(report,ensure_ascii=True))\nsys.exit(0 if report['ok'] else 1)\n"
    try:
        project = DockerProject(ROOT)
        process = subprocess.run(
            [*project.compose, "exec", "-T", "sandbox-broker", "python", "-"],
            input=source, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=args.timeout, check=False,
        )
        try:
            report = json.loads(process.stdout)
        except (ValueError, UnicodeError):
            report = {"ok": False, "error": "BROKER_PROBE_DID_NOT_RETURN_JSON", "process_exit_code": process.returncode}
        if not isinstance(report, dict):
            report = {"ok": False, "error": "INVALID_PROBE_REPORT"}
    except subprocess.TimeoutExpired:
        report = {"ok": False, "error": "PROBE_PROCESS_TIMEOUT", "note": "Inspect labeled probe resources; a killed client may interrupt cleanup."}
    except Exception as error:
        report = {"ok": False, "error": "PROBE_LAUNCH_" + type(error).__name__}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": report.get("ok", False), "passed": report.get("passed", 0), "failed": report.get("failed", 0), "error": report.get("error"), "report": str(args.output)}, ensure_ascii=True))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

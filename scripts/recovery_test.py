#!/usr/bin/env python3
"""Opt-in crash recovery test, restricted to this project's mock API replicas.

The default performs read-only preflight checks and exits unsuccessfully.
Use --inject-crash to kill the container owning this invocation's mock run.
The selected container is restarted in finally; the test thread is retained.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit
from uuid import UUID, uuid4

import httpx

if __package__:
    from .load_test import PROJECT_ROOT, ProbeError, identifier, read_auth_token, validate_state
else:
    from load_test import PROJECT_ROOT, ProbeError, identifier, read_auth_token, validate_state

PROJECT = "agent-backend-aegra"
SCOPE = (
    "Opt-in API-container crash recovery of one persisted mock I/O run; "
    "not real LLM throughput or an exactly-once side-effect guarantee."
)
INSPECT_FORMAT = (
    '{"id":{{json .Id}},"name":{{json .Name}},'
    '"hostname":{{json .Config.Hostname}},"labels":{{json .Config.Labels}},'
    '"running":{{json .State.Running}},'
    '"health":{{if .State.Health}}{{json .State.Health.Status}}{{else}}""{{end}}}'
)


def command_output(argv: list[str], *, stdin: str | None = None, timeout: float = 45) -> str:
    """Capture subprocess output without exposing command text, stdin, or stderr."""
    try:
        process = subprocess.run(
            argv, input=stdin, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        raise ProbeError("CONTAINER_COMMAND_TIMEOUT") from None
    except OSError:
        raise ProbeError("CONTAINER_COMMAND_UNAVAILABLE") from None
    if process.returncode:
        raise ProbeError("CONTAINER_COMMAND_FAILED")
    return process.stdout.strip()


def project_api(container: dict[str, Any]) -> bool:
    labels = container.get("labels")
    return isinstance(labels, dict) and (
        labels.get("com.docker.compose.project") == PROJECT
        and labels.get("com.docker.compose.service") == "api"
    )


def healthy(container: dict[str, Any]) -> bool:
    return container.get("running") is True and container.get("health") == "healthy"


def select_owner(
    claimed_by: str, containers: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Aegra 0.10.8 names workers <hostname>-<pid>-worker-<index>."""
    eligible = [container for container in containers if project_api(container)]
    matches = [
        container for container in eligible
        if isinstance(container.get("hostname"), str)
        and container["hostname"]
        and re.fullmatch(re.escape(container["hostname"]) + r"-[1-9]\d*-worker-\d+", claimed_by)
    ]
    if len(matches) != 1:
        raise ProbeError("OWNER_NOT_UNIQUELY_IDENTIFIED")
    selected = matches[0]
    if not healthy(selected):
        raise ProbeError("OWNER_CONTAINER_NOT_HEALTHY")
    others = [container for container in eligible if container["id"] != selected["id"] and healthy(container)]
    if not others:
        raise ProbeError("NO_HEALTHY_SURVIVING_REPLICA")
    return selected, others


def public_container(container: dict[str, Any]) -> dict[str, Any]:
    return {key: container[key] for key in ("id", "name", "hostname", "running", "health")}


def canonical_uuid(value: str) -> str:
    try:
        parsed = str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise ProbeError("INVALID_PROBE_UUID") from None
    if parsed != value.lower():
        raise ProbeError("INVALID_PROBE_UUID")
    return parsed


class DockerProject:
    def __init__(self, root: Path = PROJECT_ROOT):
        runtime_path = root / ".runtime.json"
        try:
            runtime = json.loads(runtime_path.read_text(encoding="utf-8-sig")) if runtime_path.exists() else {"backend": "desktop"}
        except (OSError, ValueError):
            raise ProbeError("INVALID_RUNTIME_CONFIG") from None
        if not isinstance(runtime, dict):
            raise ProbeError("INVALID_RUNTIME_CONFIG")
        backend = runtime.get("backend")
        if backend == "wsl":
            distro = runtime.get("distro")
            if not isinstance(distro, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", distro):
                raise ProbeError("INVALID_WSL_DISTRO")
            wsl = ["wsl", "-d", distro, "-u", "root", "--"]
            linux_path = command_output([*wsl, "wslpath", "-a", str(root.resolve())])
            if not linux_path.startswith("/") or "\n" in linux_path or "\x00" in linux_path:
                raise ProbeError("INVALID_WSL_PROJECT_PATH")
            self.docker = [*wsl, "/usr/bin/docker", "--host", "unix:///var/run/docker.sock"]
            compose_path = linux_path
            compose_file = linux_path.rstrip("/") + "/compose.yaml"
        elif backend == "desktop":
            self.docker = ["docker"]
            compose_path = str(root.resolve())
            compose_file = str(root.resolve() / "compose.yaml")
        else:
            raise ProbeError("UNSUPPORTED_RUNTIME_BACKEND")
        self.compose = [
            *self.docker, "compose", "--project-directory", compose_path,
            "--project-name", PROJECT, "--file", compose_file,
        ]

    def inspect(self, container_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"[0-9a-f]{12,64}", container_id):
            raise ProbeError("INVALID_CONTAINER_ID")
        output = command_output([*self.docker, "inspect", "--format", INSPECT_FORMAT, container_id])
        try:
            container = json.loads(output)
        except ValueError:
            raise ProbeError("INVALID_CONTAINER_INSPECTION") from None
        if not isinstance(container, dict) or not re.fullmatch(r"[0-9a-f]{64}", str(container.get("id", ""))):
            raise ProbeError("INVALID_CONTAINER_INSPECTION")
        if not container["id"].startswith(container_id) or not project_api(container):
            raise ProbeError("CONTAINER_OUTSIDE_PROJECT_API")
        if not all(isinstance(container.get(key), str) for key in ("name", "hostname", "health")):
            raise ProbeError("INVALID_CONTAINER_INSPECTION")
        return container

    def replicas(self) -> list[dict[str, Any]]:
        output = command_output([
            *self.docker, "ps", "--all", "--filter", f"label=com.docker.compose.project={PROJECT}",
            "--filter", "label=com.docker.compose.service=api", "--format", "{{.ID}}",
        ])
        return [self.inspect(container_id) for container_id in output.splitlines() if container_id]

    def owner(self, run_id: str, thread_id: str) -> str:
        # UUID canonicalization makes these bounded SQL literals, never arbitrary SQL input.
        run_id, thread_id = canonical_uuid(run_id), canonical_uuid(thread_id)
        query = (
            f"SELECT claimed_by FROM runs WHERE run_id = '{run_id}' "
            f"AND thread_id = '{thread_id}' AND status = 'running';"
        )
        output = command_output([
            *self.compose, "exec", "-T", "postgres", "psql", "-X", "-v", "ON_ERROR_STOP=1",
            "-U", "agents", "-d", "agents", "-At", "-c", query,
        ])
        if "\n" in output or len(output) > 512:
            raise ProbeError("INVALID_RUN_OWNER")
        return output

    def restore(self, selected: dict[str, Any], timeout: float) -> dict[str, Any]:
        current = self.inspect(selected["id"])
        if current["hostname"] != selected["hostname"]:
            raise ProbeError("RESTORE_CONTAINER_IDENTITY_CHANGED")
        command_output([*self.docker, "start", selected["id"]])
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            current = self.inspect(selected["id"])
            if healthy(current):
                return current
            time.sleep(1)
        raise ProbeError("RESTORED_CONTAINER_HEALTH_TIMEOUT")


def api_json(
    client: httpx.Client, method: str, path: str, *, retry_until: float | None = None, **kwargs: Any,
) -> Any:
    while True:
        try:
            response = client.request(method, path, **kwargs)
        except httpx.HTTPError:
            if retry_until is not None and time.monotonic() < retry_until:
                time.sleep(0.5)
                continue
            raise ProbeError("HTTP_TRANSPORT_ERROR") from None
        if response.status_code in {502, 503, 504} and retry_until is not None and time.monotonic() < retry_until:
            time.sleep(0.5)
            continue
        if response.is_error or response.is_redirect:
            raise ProbeError(f"HTTP_{response.status_code}")
        try:
            return response.json()
        except ValueError:
            raise ProbeError("INVALID_API_JSON") from None


def run_status(payload: Any) -> str:
    status = payload.get("status") if isinstance(payload, dict) else None
    if status not in {"pending", "running", "success", "error", "interrupted", "timeout"}:
        raise ProbeError("INVALID_RUN_STATUS")
    return status


def await_running(client: httpx.Client, run_path: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = run_status(api_json(client, "GET", run_path, retry_until=deadline))
        if status == "running":
            return
        if status != "pending":
            raise ProbeError("RUN_FINISHED_BEFORE_INJECTION")
        time.sleep(0.25)
    raise ProbeError("RUN_START_TIMEOUT")


# This helper runs inside the restored container and returns only selected evidence.
# The Bearer token travels in stdin, never argv, environment inspection, or output.
REREAD_CODE = '''
import json, sys, urllib.request
data = json.load(sys.stdin)
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
def get(path):
    request = urllib.request.Request("http://127.0.0.1:2026" + path,
        headers={"Authorization": "Bearer " + data["token"]})
    with opener.open(request, timeout=10) as response:
        return json.load(response)
try:
    run = get(data["run_path"])
    state = get(data["state_path"])
    values = state.get("values", {})
    assistants = [m for m in values.get("messages", []) if isinstance(m, dict)
        and (m.get("type") == "ai" or m.get("role") == "assistant")]
    content = assistants[-1].get("content", "") if assistants else ""
    if isinstance(content, list):
        content = "\\n".join(p.get("text", "") for p in content
            if isinstance(p, dict) and isinstance(p.get("text"), str))
    raw_probe = values.get("probe", {})
    probe = {k: raw_probe.get(k) for k in ("started_at", "finished_at", "instance", "request_id")}
    print(json.dumps({"run_status": run.get("status"), "probe": probe,
        "output_matches": isinstance(content, str) and data["request_id"] in content}))
except Exception:
    print(json.dumps({"error": "RESTORED_INSTANCE_API_READ_FAILED"}))
    sys.exit(1)
'''


def reread_restored(
    docker: DockerProject, selected: dict[str, Any], token: str,
    run_path: str, state_path: str, request_id: str, expected_probe: dict[str, Any],
) -> None:
    output = command_output(
        [*docker.docker, "exec", "-i", selected["id"], "python", "-c", REREAD_CODE],
        stdin=json.dumps({"token": token, "run_path": run_path, "state_path": state_path, "request_id": request_id}),
    )
    try:
        evidence = json.loads(output)
    except ValueError:
        raise ProbeError("INVALID_RESTORED_INSTANCE_EVIDENCE") from None
    if not isinstance(evidence, dict) or evidence.get("run_status") != "success" or evidence.get("output_matches") is not True:
        raise ProbeError("RESTORED_INSTANCE_PERSISTENCE_FAILED")
    if evidence.get("probe") != expected_probe:
        raise ProbeError("RESTORED_INSTANCE_STATE_CHANGED")


def execute(args: argparse.Namespace, token: str) -> dict[str, Any]:
    report: dict[str, Any] = {
        "scope": SCOPE, "project": PROJECT, "passed": False,
        "started_at": time.time(), "crash_requested": args.inject_crash,
        "crash_injected": False, "restored_healthy": False, "errors": [],
    }
    docker: DockerProject | None = None
    selected: dict[str, Any] | None = None
    attempted_crash = False
    completed_probe: dict[str, Any] | None = None
    stage = "preflight"
    with httpx.Client(
        base_url=args.base_url, headers={"Authorization": "Bearer " + token},
        timeout=10, follow_redirects=False, trust_env=False,
    ) as client:
        try:
            info = api_json(client, "GET", "/v1/info")
            if not isinstance(info, dict) or info.get("mode") != "mock":
                raise ProbeError("REFUSED_NON_MOCK_SERVICE")
            report["verified_mode"] = "mock"
            docker = DockerProject()
            replicas = docker.replicas()
            if len([container for container in replicas if healthy(container)]) < 2:
                raise ProbeError("TWO_HEALTHY_API_REPLICAS_REQUIRED")
            report["preflight_replicas"] = [public_container(container) for container in replicas]
            if not args.inject_crash:
                raise ProbeError("INJECTION_FLAG_REQUIRED")
            stage = "create_probe"
            request_id = "recovery-" + uuid4().hex
            report["request_id"] = request_id
            thread = api_json(client, "POST", "/threads", json={})
            thread_id = identifier(thread, "thread_id")
            report["thread_id"] = thread_id
            thread_path = "/threads/" + quote(thread_id, safe="")
            run = api_json(client, "POST", thread_path + "/runs", json={
                "assistant_id": "agent",
                "input": {"messages": [{"role": "user", "content": request_id}], "mock_delay_seconds": 40},
                "durability": "sync", "multitask_strategy": "reject", "stream_mode": ["values"],
            })
            run_id = identifier(run, "run_id")
            report["run_id"] = run_id
            run_path = thread_path + "/runs/" + quote(run_id, safe="")
            state_path = thread_path + "/state"
            stage = "identify_owner"
            await_running(client, run_path, min(60, args.timeout))
            owner = docker.owner(run_id, thread_id)
            selected, survivors = select_owner(owner, docker.replicas())
            # Recheck both identities, then the database claim immediately before kill.
            selected, survivors = select_owner(
                owner, [docker.inspect(container["id"]) for container in [selected, *survivors]],
            )
            if docker.owner(run_id, thread_id) != owner:
                raise ProbeError("RUN_OWNER_CHANGED_BEFORE_INJECTION")
            report["crashed_container"] = public_container(selected)
            report["claimed_by_before_crash"] = owner
            report["surviving_hostnames"] = [container["hostname"] for container in survivors]
            stage = "inject_crash"
            attempted_crash = True
            report["crash_at"] = time.time()
            command_output([*docker.docker, "kill", "--signal", "KILL", selected["id"]])
            report["crash_injected"] = True
            if docker.inspect(selected["id"])["running"]:
                raise ProbeError("KILLED_CONTAINER_STILL_RUNNING")
            print("Killed the verified owner API container; waiting for a different replica to complete the run.")
            stage = "wait_for_takeover"
            deadline = time.monotonic() + args.timeout
            last_container_check = time.monotonic()
            while time.monotonic() < deadline:
                status = run_status(api_json(client, "GET", run_path, retry_until=deadline))
                if status == "success":
                    completed_probe = validate_state(
                        api_json(client, "GET", state_path, retry_until=deadline), request_id,
                    )
                    if completed_probe["instance"] not in report["surviving_hostnames"]:
                        raise ProbeError("RUN_NOT_COMPLETED_BY_SURVIVING_REPLICA")
                    if docker.inspect(selected["id"])["running"]:
                        raise ProbeError("CRASHED_CONTAINER_RESTARTED_BEFORE_TAKEOVER")
                    report["completed_probe"] = completed_probe
                    report["recovered_before_restart"] = True
                    report["recovery_seconds"] = time.time() - report["crash_at"]
                    break
                if status in {"error", "interrupted", "timeout"}:
                    raise ProbeError("RECOVERY_RUN_" + status.upper())
                if time.monotonic() - last_container_check > 10:
                    if docker.inspect(selected["id"])["running"]:
                        raise ProbeError("CRASHED_CONTAINER_RESTARTED_BEFORE_TAKEOVER")
                    last_container_check = time.monotonic()
                time.sleep(0.5)
            else:
                raise ProbeError("RECOVERY_TIMEOUT")
        except ProbeError as exc:
            report["errors"].append({"stage": stage, "error": str(exc)})
        except KeyboardInterrupt:
            report["errors"].append({"stage": stage, "error": "INTERRUPTED"})
        except Exception as exc:
            report["errors"].append({"stage": stage, "error": "UNEXPECTED_" + type(exc).__name__})
        finally:
            if attempted_crash and docker is not None and selected is not None:
                try:
                    restored = docker.restore(selected, args.health_timeout)
                    report["restored_healthy"] = True
                    report["restored_container"] = public_container(restored)
                except ProbeError as exc:
                    report["errors"].append({"stage": "restore_container", "error": str(exc)})
                except Exception as exc:
                    report["errors"].append({"stage": "restore_container", "error": "UNEXPECTED_" + type(exc).__name__})
        if completed_probe is not None and report["restored_healthy"] and docker is not None and selected is not None:
            try:
                reread_restored(docker, selected, token, run_path, state_path, request_id, completed_probe)
                report["persisted_on_restarted_instance"] = True
            except ProbeError as exc:
                report["errors"].append({"stage": "verify_persistence", "error": str(exc)})
        report["passed"] = bool(
            not report["errors"] and report.get("recovered_before_restart")
            and report.get("persisted_on_restarted_instance") and report["restored_healthy"]
        )
        report["thread_retained"] = "thread_id" in report
        report["finished_at"] = time.time()
        return report


def arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=SCOPE)
    parser.add_argument("--inject-crash", action="store_true", help="Explicitly allow killing the verified owner API container.")
    parser.add_argument("--base-url", default=os.environ.get("BASE_URL", "http://127.0.0.1:2026"))
    parser.add_argument("--timeout", type=float, default=180, help="Maximum seconds to wait for recovery after the kill.")
    parser.add_argument("--health-timeout", type=float, default=180)
    parser.add_argument("--output", type=Path, default=Path("results/recovery.json"))
    args = parser.parse_args(argv)
    if any(not math.isfinite(value) or value <= 0 for value in (args.timeout, args.health_timeout)):
        parser.error("timeouts must be finite and positive")
    url = urlsplit(args.base_url)
    if url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost", "::1"} or url.username or url.password or url.query or url.fragment or url.path not in {"", "/"}:
        parser.error("base-url must be a local HTTP loopback URL without credentials, path, query or fragment")
    args.base_url = args.base_url.rstrip("/")
    return args


def main(argv: list[str] | None = None) -> int:
    args = arguments(argv)
    print(SCOPE)
    try:
        report = execute(args, read_auth_token())
    except ProbeError as exc:
        report = {"scope": SCOPE, "passed": False, "errors": [{"stage": "preflight", "error": str(exc)}]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if any(error["error"] == "INJECTION_FLAG_REQUIRED" for error in report["errors"]):
        print("Preflight passed. No run was created and no container was changed. Add --inject-crash to perform the crash test.")
    print(
        f"passed={report['passed']} crash_injected={report.get('crash_injected', False)} "
        f"restored_healthy={report.get('restored_healthy', False)} "
        f"persisted_on_restarted_instance={report.get('persisted_on_restarted_instance', False)}"
    )
    for error in report["errors"]:
        print(f"{error['stage']}: {error['error']}", file=sys.stderr)
    print(f"Report: {args.output}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

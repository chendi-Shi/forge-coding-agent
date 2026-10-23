#!/usr/bin/env python3
"""Verify two-user isolation and persisted mock output through the real API.

Uses two identities from .env's API_KEYS_JSON by default. AUTH_TOKEN and
SECOND_AUTH_TOKEN override the owner and second identity. This script never
stops containers or claims crash recovery; perform fault injection separately.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit
from uuid import uuid4

import httpx

if __package__:
    from .load_test import (
        PROJECT_ROOT,
        TERMINAL_STATUSES,
        ProbeError,
        identifier,
        json_request,
        read_auth_token,
        validate_state,
    )
else:
    from load_test import (
        PROJECT_ROOT,
        TERMINAL_STATUSES,
        ProbeError,
        identifier,
        json_request,
        read_auth_token,
        validate_state,
    )

SCOPE = "Live mock-only identity isolation and state reread; no process failure is injected."


def read_second_auth_token(first_token: str, env_path: Path = PROJECT_ROOT / ".env") -> str:
    override = os.environ.get("SECOND_AUTH_TOKEN", "").strip()
    if override:
        return override
    raw = os.environ.get("API_KEYS_JSON")
    if raw is None:
        try:
            lines = env_path.read_text(encoding="utf-8-sig").splitlines()
        except FileNotFoundError:
            raise ProbeError("MISSING_SECOND_AUTH_TOKEN") from None
        for line in lines:
            setting = line.strip()
            if setting.startswith("export "):
                setting = setting[7:].lstrip()
            name, separator, value = setting.partition("=")
            if separator and name.strip() == "API_KEYS_JSON":
                raw = value.strip()
                if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
                    raw = raw[1:-1]
                break
    try:
        keys = json.loads(raw or "null")
    except (ValueError, TypeError):
        raise ProbeError("INVALID_API_KEYS_JSON") from None
    if not isinstance(keys, dict):
        raise ProbeError("INVALID_API_KEYS_JSON")
    for token in keys.values():
        if isinstance(token, str) and token.strip() and token.strip() != first_token:
            return token.strip()
    raise ProbeError("MISSING_SECOND_AUTH_TOKEN")


def client_for(base_url: str, token: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=base_url,
        headers={"Authorization": "Bearer " + token},
        timeout=httpx.Timeout(20.0),
        follow_redirects=False,
        trust_env=False,
    )


def thread_path(thread_id: str) -> str:
    return "/threads/" + quote(thread_id, safe="")


async def require_mock(client: httpx.AsyncClient) -> None:
    info = await json_request(client, "GET", "/v1/info")
    if not isinstance(info, dict) or info.get("mode") != "mock":
        raise ProbeError("REFUSED_NON_MOCK_SERVICE")


async def wait_for_success(
    client: httpx.AsyncClient, path: str, poll_interval: float
) -> dict[str, Any]:
    while True:
        run = await json_request(client, "GET", path)
        if not isinstance(run, dict) or not isinstance(run.get("status"), str):
            raise ProbeError("INVALID_RUN_STATUS")
        status = run["status"]
        if status in TERMINAL_STATUSES:
            if status != "success":
                raise ProbeError("RUN_" + status.upper())
            return run
        if status not in {"pending", "running"}:
            raise ProbeError("UNKNOWN_RUN_STATUS")
        await asyncio.sleep(poll_interval)


async def execute(args: argparse.Namespace, first_token: str, second_token: str) -> dict[str, Any]:
    report: dict[str, Any] = {
        "scope": SCOPE,
        "started_at": time.time(),
        "passed": False,
        "configuration": {
            "base_url": args.base_url,
            "timeout_seconds": args.timeout,
            "mock_delay_seconds": args.delay,
        },
        "checks": [],
        "errors": [],
        "cleanup": [],
        "crash_recovery": {
            "status": "not_run",
            "reason": "No process failure or container operation is performed by this script.",
        },
    }
    # Only IDs returned by this invocation's POST /threads may be cleaned up.
    created: list[tuple[httpx.AsyncClient, str]] = []
    stage = "preflight"
    async with client_for(args.base_url, first_token) as owner, client_for(
        args.base_url, second_token
    ) as outsider:
        try:
            async with asyncio.timeout(args.timeout):
                await require_mock(owner)
                await require_mock(outsider)
                report["verified_mode"] = "mock"

                stage = "create_identity_threads"
                first = await json_request(owner, "POST", "/threads", json={})
                first_id = identifier(first, "thread_id")
                created.append((owner, first_id))
                second = await json_request(outsider, "POST", "/threads", json={})
                second_id = identifier(second, "thread_id")
                created.append((outsider, second_id))
                first_identity = identifier(first, "user_id")
                second_identity = identifier(second, "user_id")
                if first_identity == second_identity:
                    raise ProbeError("TOKENS_RESOLVE_TO_SAME_IDENTITY")
                report["checks"].append({"name": "distinct_authenticated_identities", "passed": True})
                # A valid outsider must be able to read its own resources. A 401
                # from an invalid token can never count as isolation success.
                own_second = await json_request(outsider, "GET", thread_path(second_id))
                if identifier(own_second, "user_id") != second_identity:
                    raise ProbeError("OUTSIDER_OWN_RESOURCE_MISMATCH")

                stage = "submit_owner_run"
                request_id = "reliability-" + uuid4().hex
                owner_path = thread_path(first_id)
                run = await json_request(
                    owner,
                    "POST",
                    owner_path + "/runs",
                    json={
                        "assistant_id": "agent",
                        "input": {
                            "messages": [{"role": "user", "content": request_id}],
                            "mock_delay_seconds": args.delay,
                        },
                        "stream_mode": ["values"],
                        "durability": "sync",
                    },
                )
                run_id = identifier(run, "run_id")
                run_path = owner_path + "/runs/" + quote(run_id, safe="")
                report["owner_run"] = {
                    "thread_id": first_id, "run_id": run_id, "request_id": request_id,
                }
                stage = "await_owner_run"
                await wait_for_success(owner, run_path, args.poll_interval)
                state = await json_request(owner, "GET", owner_path + "/state")
                initial_probe = validate_state(state, request_id)
                report["checks"].append({"name": "owner_run_completed", "passed": True})

                stage = "cross_identity_access"
                for name, method, path in (
                    ("outsider_read_thread", "GET", owner_path),
                    ("outsider_read_state", "GET", owner_path + "/state"),
                    ("outsider_read_run", "GET", run_path),
                    ("outsider_delete_thread", "DELETE", owner_path),
                ):
                    try:
                        response = await outsider.request(method, path)
                    except httpx.HTTPError:
                        raise ProbeError("HTTP_TRANSPORT_ERROR") from None
                    report["checks"].append({
                        "name": name,
                        "http_status": response.status_code,
                        "expected_statuses": [403, 404],
                        "passed": response.status_code in {403, 404},
                    })

                stage = "owner_reread_new_connection"
                async with client_for(args.base_url, first_token) as reconnected:
                    reread_thread = await json_request(reconnected, "GET", owner_path)
                    if identifier(reread_thread, "user_id") != first_identity:
                        raise ProbeError("OWNER_IDENTITY_CHANGED")
                    reread_run = await json_request(reconnected, "GET", run_path)
                    if reread_run.get("status") != "success":
                        raise ProbeError("OWNER_RUN_NO_LONGER_SUCCESSFUL")
                    reread_state = await json_request(reconnected, "GET", owner_path + "/state")
                    reread_probe = validate_state(reread_state, request_id)
                    if reread_probe != initial_probe:
                        raise ProbeError("PERSISTED_PROBE_CHANGED")
                report["checks"].append({
                    "name": "owner_output_unchanged_after_denied_access_and_reconnect",
                    "passed": True,
                })
                report["state_reread"] = {
                    "status": "passed",
                    "scope": "New HTTP connection; does not prove restart or crash recovery.",
                    "probe": reread_probe,
                }
        except TimeoutError:
            report["errors"].append({"stage": stage, "error": "TEST_DEADLINE_EXCEEDED"})
        except ProbeError as exc:
            report["errors"].append({"stage": stage, "error": str(exc)})
        finally:
            for client, owned_id in created:
                try:
                    response = await client.delete(thread_path(owned_id))
                    # 404 is safe cleanup if the isolation failure already removed it;
                    # the earlier failed check still makes the report fail.
                    report["cleanup"].append({
                        "thread_id": owned_id,
                        "http_status": response.status_code,
                        "passed": response.is_success or response.status_code == 404,
                    })
                except httpx.HTTPError:
                    report["cleanup"].append({
                        "thread_id": owned_id, "error": "HTTP_TRANSPORT_ERROR", "passed": False,
                    })
    report["passed"] = (
        len(report["checks"]) == 7
        and not report["errors"]
        and all(item["passed"] for item in report["checks"])
        and all(item["passed"] for item in report["cleanup"])
    )
    report["finished_at"] = time.time()
    return report


def arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=SCOPE)
    parser.add_argument("--base-url", default=os.environ.get("BASE_URL", "http://127.0.0.1:2026"))
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--delay", type=float, default=0.1)
    parser.add_argument("--poll-interval", type=float, default=0.2)
    parser.add_argument("--output", type=Path, default=Path("results/reliability.json"))
    args = parser.parse_args(argv)
    if any(not math.isfinite(x) or x <= 0 for x in (args.timeout, args.delay, args.poll_interval)):
        parser.error("timeout, delay and poll-interval must be finite and positive")
    if args.delay > 60:
        parser.error("delay must be at most 60 seconds")
    url = urlsplit(args.base_url)
    if url.scheme not in {"http", "https"} or not url.netloc or url.username or url.password or url.query or url.fragment:
        parser.error("base-url must be an HTTP(S) URL without credentials, query or fragment")
    args.base_url = args.base_url.rstrip("/")
    return args


def main(argv: list[str] | None = None) -> int:
    args = arguments(argv)
    try:
        first_token = read_auth_token()
        second_token = read_second_auth_token(first_token)
        if first_token == second_token:
            raise ProbeError("SECOND_AUTH_TOKEN_MUST_DIFFER")
        report = asyncio.run(execute(args, first_token, second_token))
    except ProbeError as exc:
        report = {"scope": SCOPE, "passed": False, "errors": [{"stage": "preflight", "error": str(exc)}]}
    except KeyboardInterrupt:
        print("Interrupted; this invocation's threads may remain for inspection.", file=sys.stderr)
        return 130
    except Exception as exc:
        # Never print response bodies, exception strings, headers or credentials.
        report = {"scope": SCOPE, "passed": False, "errors": [{
            "stage": "runtime", "error": "UNEXPECTED_RUNTIME_ERROR", "type": type(exc).__name__,
        }]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"passed={report['passed']} checks={len(report.get('checks', []))}; crash recovery not tested")
    print(f"Report: {args.output}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Measure concurrent mock I/O runs; this is not a real LLM throughput benchmark.

Run from the project directory: python scripts/load_test.py --concurrency 100
Only httpx is required beyond Python 3.11's standard library.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit
from uuid import uuid4

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TERMINAL_STATUSES = {"success", "error", "interrupted", "timeout"}
SCOPE = "Mock I/O concurrency validation only; not real LLM throughput."


class ProbeError(Exception):
    """A controlled error code safe to include in reports."""


def percentile(values: Iterable[float], fraction: float) -> float | None:
    ordered = sorted(values)
    if not ordered:
        return None
    position = (len(ordered) - 1) * fraction
    low, high = math.floor(position), math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def peak_running(intervals: Iterable[tuple[float, float]]) -> int:
    """Count half-open [start, finish) intervals; finishes sort before starts."""
    events: list[tuple[float, int]] = []
    for start, finish in intervals:
        if not math.isfinite(start) or not math.isfinite(finish) or finish < start:
            raise ValueError("invalid execution interval")
        if finish == start:
            continue
        events.extend(((start, 1), (finish, -1)))
    running = peak = 0
    for _, change in sorted(events):
        running += change
        peak = max(peak, running)
    return peak


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    return ""


def validate_state(state: Any, request_id: str) -> dict[str, Any]:
    """Verify both the output message and the server's per-request timing."""
    if not isinstance(state, dict) or not isinstance(state.get("values"), dict):
        raise ProbeError("INVALID_STATE")
    values = state["values"]
    probe = values.get("probe")
    if not isinstance(probe, dict) or probe.get("request_id") != request_id:
        raise ProbeError("PROBE_REQUEST_ID_MISMATCH")
    for field in ("started_at", "finished_at"):
        value = probe.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ProbeError("INVALID_SERVER_TIMING")
        if not math.isfinite(value):
            raise ProbeError("INVALID_SERVER_TIMING")
    if probe["finished_at"] < probe["started_at"]:
        raise ProbeError("INVALID_SERVER_TIMING")
    instance = probe.get("instance")
    if not isinstance(instance, str) or not instance or len(instance) > 256:
        raise ProbeError("INVALID_INSTANCE")
    messages = values.get("messages")
    if not isinstance(messages, list):
        raise ProbeError("MISSING_OUTPUT_MESSAGE")
    assistant_messages = [
        message
        for message in messages
        if isinstance(message, dict)
        and (message.get("type") == "ai" or message.get("role") == "assistant")
    ]
    if not assistant_messages or request_id not in _message_text(
        assistant_messages[-1].get("content")
    ):
        raise ProbeError("OUTPUT_REQUEST_ID_MISMATCH")
    return {
        "started_at": float(probe["started_at"]),
        "finished_at": float(probe["finished_at"]),
        "instance": instance,
        "request_id": request_id,
    }


def read_auth_token(env_path: Path = PROJECT_ROOT / ".env") -> str:
    token = os.environ.get("AUTH_TOKEN", "").strip()
    if token:
        return token
    # Read this one setting only. Never echo the file, parsed values, or token.
    try:
        lines = env_path.read_text(encoding="utf-8-sig").splitlines()
    except FileNotFoundError:
        raise ProbeError("MISSING_AUTH_TOKEN") from None
    raw: str | None = None
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
        if not isinstance(keys, dict) or not keys:
            raise ValueError
        token = next(iter(keys.values()))
        if not isinstance(token, str) or not token.strip():
            raise ValueError
        return token.strip()
    except (ValueError, TypeError):
        raise ProbeError("INVALID_API_KEYS_JSON") from None


def identifier(payload: Any, key: str) -> str:
    value = payload.get(key) if isinstance(payload, dict) else None
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ProbeError("INVALID_" + key.upper())
    return value


async def json_request(
    client: httpx.AsyncClient, method: str, path: str, **kwargs: Any
) -> Any:
    try:
        response = await client.request(method, path, **kwargs)
    except httpx.TimeoutException:
        raise ProbeError("HTTP_TIMEOUT") from None
    except httpx.HTTPError:
        raise ProbeError("HTTP_TRANSPORT_ERROR") from None
    if response.is_error:
        raise ProbeError(f"HTTP_{response.status_code}")
    try:
        return response.json()
    except ValueError:
        raise ProbeError("INVALID_JSON_RESPONSE") from None


async def create_thread(client: httpx.AsyncClient) -> dict[str, Any]:
    result: dict[str, Any] = {"request_id": "probe-" + uuid4().hex}
    try:
        payload = await json_request(client, "POST", "/threads", json={})
        result["thread_id"] = identifier(payload, "thread_id")
    except ProbeError as exc:
        result.update(status="failed", stage="create_thread", error=str(exc))
    return result


async def run_probe(
    client: httpx.AsyncClient,
    result: dict[str, Any],
    barrier: asyncio.Barrier,
    *,
    delay: float,
    deadline_seconds: float,
    poll_interval: float,
) -> dict[str, Any]:
    await barrier.wait()
    started = time.perf_counter()
    stage = "submit_run"
    thread_path = "/threads/" + quote(result["thread_id"], safe="")
    try:
        async with asyncio.timeout(deadline_seconds):
            payload = await json_request(
                client,
                "POST",
                thread_path + "/runs",
                json={
                    "assistant_id": "agent",
                    "input": {
                        "messages": [{"role": "user", "content": result["request_id"]}],
                        "mock_delay_seconds": delay,
                    },
                    "stream_mode": ["values"],
                },
            )
            result["run_id"] = identifier(payload, "run_id")
            stage = "poll_run"
            run_path = thread_path + "/runs/" + quote(result["run_id"], safe="")
            while True:
                run = await json_request(client, "GET", run_path)
                if not isinstance(run, dict) or not isinstance(run.get("status"), str):
                    raise ProbeError("INVALID_RUN_STATUS")
                status = run["status"]
                if status in TERMINAL_STATUSES:
                    if status != "success":
                        raise ProbeError("RUN_" + status.upper())
                    break
                if status not in {"pending", "running"}:
                    raise ProbeError("UNKNOWN_RUN_STATUS")
                await asyncio.sleep(poll_interval)
            stage = "validate_output"
            state = await json_request(client, "GET", thread_path + "/state")
            result["probe"] = validate_state(state, result["request_id"])
            result["status"] = "success"
    except TimeoutError:
        result.update(status="failed", stage=stage, error="RUN_DEADLINE_EXCEEDED")
    except ProbeError as exc:
        result.update(status="failed", stage=stage, error=str(exc))
    result["end_to_end_seconds"] = round(time.perf_counter() - started, 6)
    return result


def summarize(results: list[dict[str, Any]], min_peak: int) -> dict[str, Any]:
    successful = [item for item in results if item.get("status") == "success"]
    errors = [
        {key: item[key] for key in ("request_id", "thread_id", "run_id", "stage", "error") if key in item}
        for item in results
        if item.get("status") != "success"
    ]
    intervals = [
        (item["probe"]["started_at"], item["probe"]["finished_at"])
        for item in successful
    ]
    instances: dict[str, dict[str, int]] = {}
    for item in successful:
        instance = item["probe"]["instance"]
        instances.setdefault(instance, {"successful_runs": 0, "peak_running": 0})
        instances[instance]["successful_runs"] += 1
    for instance, counts in instances.items():
        counts["peak_running"] = peak_running(
            (item["probe"]["started_at"], item["probe"]["finished_at"])
            for item in successful
            if item["probe"]["instance"] == instance
        )
    peak = peak_running(intervals)
    latencies = [item["end_to_end_seconds"] for item in successful]
    return {
        "success": len(successful),
        "failed": len(errors),
        "errors": errors,
        "peak_running": peak,
        "minimum_peak_required": min_peak,
        "peak_requirement_met": peak >= min_peak,
        "p50_end_to_end_seconds": percentile(latencies, 0.50),
        "p95_end_to_end_seconds": percentile(latencies, 0.95),
        "latency_scope": "Successful runs: barrier release through final output validation; excludes thread creation.",
        "peak_scope": "Overlap of validated successful runs' server timestamps; synchronized clocks required across instances.",
        "instances": instances,
        "passed": not errors and peak >= min_peak,
    }


async def execute(args: argparse.Namespace, token: str) -> dict[str, Any]:
    report: dict[str, Any] = {
        "scope": SCOPE,
        "started_at": time.time(),
        "configuration": {
            "base_url": args.base_url,
            "concurrency": args.concurrency,
            "mock_delay_seconds": args.delay,
            "timeout_seconds": args.timeout,
            "poll_interval_seconds": args.poll_interval,
        },
        "cleanup_requested": args.cleanup,
        "runs": [],
    }
    limits = httpx.Limits(
        max_connections=max(128, args.concurrency * 2),
        max_keepalive_connections=max(128, args.concurrency),
    )
    async with httpx.AsyncClient(
        base_url=args.base_url,
        headers={"Authorization": "Bearer " + token},
        limits=limits,
        timeout=httpx.Timeout(min(30.0, args.timeout)),
        follow_redirects=False,
        trust_env=False,
    ) as client:
        info = await json_request(client, "GET", "/v1/info")
        if not isinstance(info, dict) or info.get("mode") != "mock":
            raise ProbeError("REFUSED_NON_MOCK_SERVICE")
        report["verified_mode"] = "mock"
        results = await asyncio.gather(*(create_thread(client) for _ in range(args.concurrency)))
        # Every thread is created before any run is submitted. Each run owns a thread.
        created = [item for item in results if "thread_id" in item]
        barrier = asyncio.Barrier(len(created) + 1)
        tasks = [
            asyncio.create_task(
                run_probe(
                    client,
                    item,
                    barrier,
                    delay=args.delay,
                    deadline_seconds=args.timeout,
                    poll_interval=args.poll_interval,
                )
            )
            for item in created
        ]
        await barrier.wait()
        await asyncio.gather(*tasks)
        report["runs"] = results
        report.update(summarize(results, args.min_peak))
        if args.cleanup:
            cleanup_errors: list[dict[str, str]] = []

            async def delete_created(item: dict[str, Any]) -> None:
                try:
                    response = await client.delete("/threads/" + quote(item["thread_id"], safe=""))
                    if response.is_error:
                        raise ProbeError(f"HTTP_{response.status_code}")
                except (httpx.HTTPError, ProbeError) as exc:
                    code = str(exc) if isinstance(exc, ProbeError) else "HTTP_TRANSPORT_ERROR"
                    cleanup_errors.append({"thread_id": item["thread_id"], "error": code})

            await asyncio.gather(*(delete_created(item) for item in created))
            report["cleanup_errors"] = cleanup_errors
            report["threads_deleted"] = len(created) - len(cleanup_errors)
            if cleanup_errors:
                report["passed"] = False
        else:
            report["threads_retained"] = len(created)
    report["finished_at"] = time.time()
    return report


def arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=SCOPE)
    parser.add_argument("--base-url", default=os.environ.get("BASE_URL", "http://127.0.0.1:2026"))
    parser.add_argument("--concurrency", type=int, default=100)
    parser.add_argument("--delay", type=float, default=10.0)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--poll-interval", type=float, default=0.25)
    parser.add_argument("--min-peak", type=int, default=100)
    parser.add_argument("--output", type=Path, default=Path("results/load-test.json"))
    parser.add_argument("--cleanup", action="store_true", help="Delete only the threads created by this invocation.")
    args = parser.parse_args(argv)
    if args.concurrency < 1 or not 1 <= args.min_peak <= args.concurrency:
        parser.error("concurrency must be positive and 1 <= min-peak <= concurrency")
    if any(not math.isfinite(x) or x <= 0 for x in (args.delay, args.timeout, args.poll_interval)):
        parser.error("delay, timeout and poll-interval must be finite positive numbers")
    url = urlsplit(args.base_url)
    if url.scheme not in {"http", "https"} or not url.netloc or url.username or url.password or url.query or url.fragment:
        parser.error("base-url must be an HTTP(S) URL without credentials, query or fragment")
    args.base_url = args.base_url.rstrip("/")
    return args


def main(argv: list[str] | None = None) -> int:
    args = arguments(argv)
    print(SCOPE)
    try:
        token = read_auth_token()
        report = asyncio.run(execute(args, token))
    except ProbeError as exc:
        report = {"scope": SCOPE, "passed": False, "success": 0, "errors": [{"stage": "preflight", "error": str(exc)}]}
    except KeyboardInterrupt:
        print("Interrupted; this invocation's created threads may remain for inspection.", file=sys.stderr)
        return 130
    # Reports contain only timing, identifiers, controlled errors and aggregate metrics.
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(
        f"passed={report['passed']} success={report.get('success', 0)}/{args.concurrency} "
        f"peak_running={report.get('peak_running', 0)} required={args.min_peak} "
        f"p50={report.get('p50_end_to_end_seconds')}s p95={report.get('p95_end_to_end_seconds')}s"
    )
    print(f"Report: {args.output}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

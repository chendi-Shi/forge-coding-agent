"""Real Forge repair of a review finding. Retains the original pilot result."""

import argparse
import asyncio
import json
import time
from pathlib import Path

import httpx
from coding_worker import read_auth_token

ROOT = Path(__file__).resolve().parents[1]
PROMPT = """Review found a compatibility regression in the previous autospec fix. Repair it while preserving the original issue443 fix and real caching behavior. The previous patch made _MethodDescriptor.__class__ claim types.FunctionType. The following three checks each raise AttributeError about missing __code__ in the current candidate, but all succeed in the original parent implementation:

import inspect
from unittest import mock
from cachetools import cachedmethod
class Service:
    def __init__(self):
        self.cache = {}
    @cachedmethod(lambda self: self.cache)
    def cached(self, key):
        return key
raw = inspect.getattr_static(Service, 'cached')
mock.create_autospec(raw)
assert inspect.iscoroutinefunction(raw) is False
assert inspect.isgeneratorfunction(raw) is False

Check each operation independently so the first exception does not hide the others. Add regression coverage in a NEW test file. Do not delete, weaken, or modify existing tests, including the previous task's new tests. Keep class/instance autospec, keyword and positional calls, inheritance, info/lock/condition, actual cache behavior and class access working. Read the implementation, reproduce, fix the source design and run all relevant tests. Do not merely suppress exceptions or remove the original fix.
"""
TEST_REPAIR = """The prior source repair already passes the frozen issue443 tests and all three descriptor introspection checks, but the previous Agent added an incorrect regression assertion. Complete this focused follow-up by reproducing and correcting the NEW test, then running the full fixed verification.

File: tests/test_cachedmethod_descriptor_inspection.py, test_inspection_preserves_cache_behavior, all six service_class variants.
It calls service_class.cached(service, 'key'), service.cached(key='key'), then service_class.cached(self=service, key='key'), but expects service.calls == 1. Independently running this same newly added test on the upstream reference gives 6 failures with calls == 2: default methodkey distinguishes positional arguments from keyword arguments. First positional and first keyword calls are separate misses; the second keyword call is a hit. This is established cache semantics, not a regression in the new descriptor source design.

You may correct the erroneous expectations ONLY in the previous Agent's new tests/test_cachedmethod_descriptor_inspection.py. Preserve its coverage and validate cache-hit behavior correctly; do not delete tests, mark them skipped, weaken assertions arbitrarily or change any original upstream test or tests/test_cachedmethod_autospec.py. Avoid changing the source cache-key semantics to satisfy the wrong test. Read the new test, reproduce the precise failure, make the targeted correction and run python -m pytest -q -o pythonpath=src tests/test_cachedmethod*.py. Finally explain the correction and actual results.
"""


async def main(label="followup"):
    root = ROOT / "results"
    active_path = root / f"forge-{label}-active.json"
    parent = json.loads((root / "forge-issues-active.json").read_text())["cachetools-autospec"]
    if label == "test-repair":
        parent["task_id"] = json.loads((root / "forge-memory-retry-active.json").read_text())["task_id"]
    async with httpx.AsyncClient(base_url="http://127.0.0.1:2026/api/coding", headers={"Authorization": "Bearer " + read_auth_token()}, timeout=300, trust_env=False) as client:
        if active_path.exists():
            identifier = json.loads(active_path.read_text())["task_id"]
        else:
            response = await client.post("/tasks", json={"project_id": parent["project_id"], "parent_task_id": parent["task_id"],
                "engine": "forge", "prompt": TEST_REPAIR if label == "test-repair" else PROMPT, "timeout_seconds": 1200,
                "test_command": "python -m pytest -q -o pythonpath=src tests/test_cachedmethod*.py"})
            response.raise_for_status()
            identifier = response.json()["id"]
            active_path.write_text(json.dumps({"task_id": identifier, "parent_task_id": parent["task_id"]}), encoding="utf-8")
            print(json.dumps({"submitted_followup": identifier}), flush=True)
        deadline, prior = time.monotonic() + 1300, None
        while time.monotonic() < deadline:
            # A deployment can briefly interrupt this read-only observer. The
            # saved task ID makes restarting safe without another model job.
            try:
                response = await client.get("/tasks/" + identifier)
            except httpx.TransportError:
                await asyncio.sleep(5)
                continue
            if response.status_code in {502, 503, 504}:
                await asyncio.sleep(5)
                continue
            response.raise_for_status()
            task = response.json()
            if task["status"] != prior:
                print(json.dumps({"status": task["status"]}), flush=True)
                prior = task["status"]
            if task["status"] not in {"running", "queued", "verifying"}:
                break
            await asyncio.sleep(5)
        else:
            raise TimeoutError(f"Task {identifier} still pending; rerun to observe the same task")
        archive = await client.get(f"/tasks/{identifier}/archive")
        archive.raise_for_status()
        (root / f"forge-cachetools-{label}.zip").write_bytes(archive.content)
        (root / f"forge-cachetools-{label}.patch").write_text(task["diff"], encoding="utf-8")
        events, cursor = [], 0
        while True:
            batch = (await client.get(f"/tasks/{identifier}/events", params={"after": cursor})).json()["events"]
            events.extend(batch)
            if len(batch) < 200:
                break
            cursor = batch[-1]["id"]
        (root / f"forge-cachetools-{label}-events.json").write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8")
        (root / f"forge-cachetools-{label}.json").write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"status": task["status"], "error": task["error"], "usage": task["usage"]}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-label", choices=["followup", "memory-retry", "test-repair"], default="followup")
    asyncio.run(main(parser.parse_args().run_label))

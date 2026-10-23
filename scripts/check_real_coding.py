"""Two real Codex jobs, with Docker verification and unchanged-test assertions.

Consumes the configured Codex account's model usage. Resumes saved task IDs on
rerun rather than submitting duplicates. Results remain awaiting human review.
"""

import asyncio
import io
import json
import time
import zipfile
from datetime import datetime
from pathlib import Path

import httpx

from agent_backend.coding.sample import SAMPLE_FILES

ROOT = Path(__file__).resolve().parents[1]
NODE_FILES = {
    "README.md": "# Pagination utility\nOne-based pages. Positive integer page and size required. Empty input has zero pages. Out-of-range pages return no items. Run node --test test/paginate.test.cjs.\n",
    "paginate.cjs": "exports.paginate = function(items, page, size) { return {items: items.slice(page * size, (page + 1) * size), totalPages: Math.floor(items.length / size), total: items.length}; };\n",
    "test/paginate.test.cjs": """const test = require('node:test');
const assert = require('node:assert/strict');
const {paginate} = require('../paginate.cjs');
test('first page is one based', () => assert.deepEqual(paginate([1,2,3,4,5],1,2), {items:[1,2],totalPages:3,total:5}));
test('last partial page', () => assert.deepEqual(paginate([1,2,3,4,5],3,2).items,[5]));
test('out of range', () => assert.deepEqual(paginate([1,2],4,2).items,[]));
test('empty collection', () => assert.deepEqual(paginate([],1,2), {items:[],totalPages:0,total:0}));
test('reject invalid pagination', () => {for (const [page,size] of [[0,2],[-1,2],[1,0],[1,-2],[1.5,2],[1,1.5],[NaN,2],[1,Infinity]]) assert.throws(() => paginate([1,2],page,size), RangeError);});
test('input is not changed', () => {const items=[1,2,3]; paginate(items,1,2); assert.deepEqual(items,[1,2,3]);});
""",
    ".gitignore": "node_modules/\n",
}


async def main():
    results = ROOT / "results"
    results.mkdir(exist_ok=True)
    active_path = results / "real-coding-active.json"
    env = dict(line.split("=", 1) for line in (ROOT / ".env").read_text().splitlines() if "=" in line and not line.startswith("#"))
    key = json.loads(env["API_KEYS_JSON"])["local-client"]
    async with httpx.AsyncClient(base_url="http://127.0.0.1:2026/api/coding", headers={"Authorization": f"Bearer {key}"}, timeout=300, trust_env=False) as client:
        async def post(path, body=None):
            response = await client.post(path, json=body or {})
            response.raise_for_status()
            return response.json()

        if active_path.exists():
            ids = json.loads(active_path.read_text())
        else:
            python = await post("/projects/sample")
            node = await post("/projects", {"name": "分页工具 · Node.js", "files": NODE_FILES, "test_command": "node --test test/paginate.test.cjs"})
            jobs = await asyncio.gather(post("/tasks", {"project_id": python["id"], "prompt": python["suggested_prompt"]}), post("/tasks", {"project_id": node["id"], "prompt": "修复 paginate 分页函数的边界错误。阅读 README 和测试，复现失败，修复实现并运行全部现有测试。不删除、不修改或放宽测试，最后说明修复内容。"}))
            ids = {"python": jobs[0]["id"], "node": jobs[1]["id"]}
            active_path.write_text(json.dumps(ids), encoding="utf-8")
            print(json.dumps({"submitted_real_codex_tasks": ids}), flush=True)
        deadline = time.monotonic() + 1000
        last = None
        while time.monotonic() < deadline:
            tasks = [(await client.get(f"/tasks/{identifier}")).json() for identifier in ids.values()]
            statuses = [task["status"] for task in tasks]
            if statuses != last:
                print(json.dumps({"states": statuses}), flush=True)
                last = statuses
            if all(status not in {"queued", "running", "verifying"} for status in statuses):
                break
            await asyncio.sleep(2)
        else:
            raise RuntimeError("Real coding validation exceeded 1000 seconds; task IDs retained for inspection.")
        reports, spans = [], []
        for kind, task in zip(ids, tasks, strict=True):
            assert task["status"] in {"awaiting_review", "accepted"}, {"id": task["id"], "status": task["status"], "error": task["error"]}
            assert task["baseline_test"]["exit_code"] != 0, task["baseline_test"]
            assert task["verification"]["exit_code"] == 0, task["verification"]
            assert task["session_id"] and task["diff"] and task["usage"]
            response = await client.get(f"/tasks/{task['id']}/archive")
            response.raise_for_status()
            expected = SAMPLE_FILES if kind == "python" else NODE_FILES
            test_path = "tests/test_orders.py" if kind == "python" else "test/paginate.test.cjs"
            with zipfile.ZipFile(io.BytesIO(response.content)) as content:
                assert content.read(test_path).decode() == expected[test_path], "Existing tests were changed"
            events, cursor = [], 0
            while True:
                batch = (await client.get(f"/tasks/{task['id']}/events?after={cursor}")).json()["events"]
                events.extend(batch)
                if len(batch) < 200:
                    break
                cursor = batch[-1]["id"]
            types = [event["payload"].get("item", {}).get("type") for event in events]
            assert "command_execution" in types and "file_change" in types, types
            start = next(event["created_at"] for event in events if event["kind"] == "thread.started")
            end = next(event["created_at"] for event in events if event["kind"] == "turn.completed")
            spans.append((datetime.fromisoformat(start), datetime.fromisoformat(end)))
            reports.append({"language": kind, "task_id": task["id"], "session_id": task["session_id"], "status": task["status"], "baseline_exit": task["baseline_test"]["exit_code"], "verification_exit": task["verification"]["exit_code"], "existing_tests_unchanged": True, "events": len(events), "diff_bytes": len(task["diff"].encode()), "usage": task["usage"], "summary": task["summary"]})
        overlap = max(0, (min(span[1] for span in spans) - max(span[0] for span in spans)).total_seconds())
        report = {"real_codex_jobs_passed": len(reports), "model_execution_overlap_seconds": overlap, "jobs": reports}
        (results / "real-coding.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    asyncio.run(main())

"""Replay actual cachetools bug candidates through one deployed acceptance policy.

Uses the frozen issue443 parent, the first model candidate that passed the original
holdout but failed post-hoc review, and the reviewed repair candidate. These tests
are now feedback-aware regression tests, not a fresh blind evaluation or new model
attempt. No reference implementation enters any task workspace; no model is called.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
from check_acceptance import ROOT, Harness, ProbeFailure, ensure, isolated_token

BENCHMARK = ROOT / "benchmarks/cachetools-autospec"
CANDIDATES = [
    ("upstream_parent", BENCHMARK / "project.zip", 4),
    ("first_model_candidate_review_failure", ROOT / "results/forge-cachetools-autospec.zip", 3),
    ("reviewed_final_repair_candidate", ROOT / "results/forge-cachetools-test-repair.zip", 0),
]


def unzip(path):
    data = path.read_bytes()
    with zipfile.ZipFile(io.BytesIO(data)) as content:
        return data, {name: content.read(name) for name in content.namelist() if not name.endswith("/")}


async def main(args):
    if args.dry_run:
        print(json.dumps({"cases": [name for name, _, _ in CANDIDATES], "real_model_calls": 0,
                          "executes_requests": False, "blind_evaluation": False}, indent=2))
        return 0
    output = await asyncio.to_thread(Path(args.report).resolve)
    if output.exists():
        previous = output.with_name(output.stem + "-previous-" + uuid4().hex[:8] + ".json")
        output.replace(previous)
    inputs = [(name, *unzip(path), expected) for name, path, expected in CANDIDATES]
    source_zip, baseline = inputs[0][1:3]
    protected = sorted(path for path in baseline if path.startswith("tests/"))
    async with httpx.AsyncClient(
        base_url=args.base_url.rstrip("/") + "/api/coding", timeout=300, trust_env=False,
        headers={"Authorization": "Bearer " + isolated_token()},
    ) as client:
        harness = Harness(client, output)
        harness.report.update(
            scope="Real deployed API/PostgreSQL/Docker acceptance of three existing cachetools issue443 candidates; no model calls.",
            blind_evaluation=False,
            parent_commit="eba200635273fe45b67e669b84cb45f995b27c24",
            issue_url="https://github.com/tkem/cachetools/issues/443",
            evaluation_note="Frozen issue oracle plus previously disclosed post-hoc compatibility tests and candidate-origin assertion. Does not revise earlier scores or count as fresh model success.",
        )
        try:
            await harness.preflight()
            hidden = {
                "test_issue443_private.py": (BENCHMARK / "oracle/test_regression.py").read_text(encoding="utf-8"),
                "test_review_private.py": (BENCHMARK / "post_hoc_review/test_descriptor_introspection.py").read_text(encoding="utf-8"),
                "test_candidate_origin.py": (
                    "from pathlib import Path\nimport cachetools\n\n"
                    "def test_imported_code_is_the_candidate():\n"
                    "    assert Path(cachetools.__file__).resolve().is_relative_to(Path('/workspace/src/cachetools'))\n"
                ),
            }
            hidden = {path: f"# {harness.marker}\n{source}" for path, source in hidden.items()}
            harness.private_paths = set(hidden)
            project = await harness.post("/projects", {
                "name": "OSS acceptance regression · cachetools " + uuid4().hex[:6],
                "archive_base64": base64.b64encode(source_zip).decode(), "test_command": "true",
            })
            suite = await harness.post(f"/projects/{project['id']}/acceptance-suites", {
                "name": "Cachetools issue443 + disclosed descriptor review", "files": hidden,
                "test_paths": sorted(hidden), "python_paths": ["src"], "protected_paths": protected,
                "minimum_tests": 11, "timeout_seconds": 60,
            })
            harness.check_private(suite)
            harness.report.update(project_id=project["id"], suite_id=suite["id"], policy_digest=suite["digest"])
            harness.save()
            for name, data, files, expected_failed in inputs:
                async def run_candidate(case, data=data, files=files, expected_failed=expected_failed):
                    case["input_archive_sha256"] = hashlib.sha256(data).hexdigest()
                    ensure(set(baseline).issubset(files), "CANDIDATE_REMOVES_BASELINE_FILES")
                    ensure(all(files[path] == baseline[path] for path in protected), "CANDIDATE_CHANGED_ORIGINAL_TESTS")
                    lease = await harness.start(project, case)
                    changes = [path for path in sorted(files) if files[path] != baseline.get(path)]
                    for path in changes:
                        await harness.write(lease, path, files[path].decode("utf-8"))
                    case["changed_paths"] = changes
                    await harness.finish(lease, case, expected_pass=expected_failed == 0)
                    tests = case["verification"]["acceptance"]["tests"]
                    ensure(tests["collected"] == 11 and tests["failed"] == expected_failed
                           and tests["passed"] == 11 - expected_failed, "REAL_OSS_REGRESSION_COUNTS_CHANGED")
                    if expected_failed:
                        await harness.refused(lease["id"], case)
                    else:
                        accepted = await harness.post(f"/tasks/{lease['id']}/accept")
                        ensure(accepted["status"] == "accepted", "REVIEWED_OSS_CANDIDATE_NOT_ACCEPTED")
                        case["accept_status"] = 200
                await harness.case(name, run_candidate)
            harness.report["finished_at"] = datetime.now(UTC).isoformat()
            harness.report["all_passed"] = len(harness.report["cases"]) == 3 and all(case["passed"] for case in harness.report["cases"])
            harness.save()
        except (ProbeFailure, httpx.HTTPError, KeyError, ValueError) as error:
            harness.report["fatal_error"] = str(error) if isinstance(error, ProbeFailure) else type(error).__name__
            harness.save()
            await harness.cleanup_failed_case()
        passed = harness.report["all_passed"]
        print(json.dumps({"all_passed": passed, "checks": len(harness.report["cases"]), "real_model_calls": 0, "blind_evaluation": False}), flush=True)
        return 0 if passed else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:2026")
    parser.add_argument("--report", default=str(ROOT / "results/acceptance-oss-live.json"))
    parser.add_argument("--dry-run", action="store_true")
    raise SystemExit(asyncio.run(main(parser.parse_args())))

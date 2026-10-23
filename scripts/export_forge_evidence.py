"""Export a credential-free evidence snapshot from the recorded local pilot.

Reads only the named evaluation reports, never .env, worker credentials or README.
Preserves failed attempts and checks that final grading matches candidate bytes.
"""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "results"


def read(name):
    return json.loads((RESULTS / name).read_text(encoding="utf-8"))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compact_task(task):
    keys = ("id", "task_id", "parent_task_id", "status", "error", "usage", "started_at",
            "finished_at", "checkpoint_revision", "verification", "events")
    result = {key: task[key] for key in keys if key in task}
    if result.get("started_at") and result.get("finished_at"):
        result["wall_seconds_including_outages"] = round((datetime.fromisoformat(result["finished_at"])
                                                        - datetime.fromisoformat(result["started_at"])).total_seconds(), 3)
    return result


def main():
    final_name = "forge-cachetools-test-repair"
    final_hash = digest(RESULTS / (final_name + ".zip"))
    for suffix in ("-oracle.json", "-review.json", "-change-review.json"):
        report = read(final_name + suffix)
        row = report[0] if isinstance(report, list) else report
        if row["candidate_sha256"] != final_hash:
            raise ValueError(f"Stale grading report: {suffix}")
    report_names = [
        "forge-cachetools-autospec-oracle.json", "forge-cachetools-autospec-review-first.json",
        "forge-boltons-pearson-oracle.json", "forge-cachetools-memory-retry-oracle.json",
        "forge-cachetools-memory-retry-review.json",
        "forge-cachetools-memory-retry-frozen-and-test-review.json",
        "forge-cachetools-test-repair-pre-resume-oracle.json",
        "forge-cachetools-test-repair-pre-resume-review.json",
        "forge-cachetools-test-repair-pre-resume-change-review.json",
        "forge-cachetools-test-repair-oracle.json", "forge-cachetools-test-repair-review.json",
        "forge-cachetools-test-repair-change-review.json",
        "forge-control.json", "forge-recovery.json", "coding-queue.json",
    ]
    initial = {name: compact_task(task) for name, task in read("forge-issues.json").items()}
    initial["cachetools-autospec"]["later_review_outcome"] = "rejected: three introspection regressions"
    attempts = [compact_task(read(f"forge-cachetools-{label}.json"))
                for label in ("followup", "memory-retry", "test-repair")]
    ui = read("studio-deployed-final/studio-check.json")
    snapshot = {
        "exported_at": datetime.now(UTC).isoformat(),
        "scope": "Two selected historical OSS issues; not SWE-bench or a general success-rate estimate.",
        "provider": "Codex decision bridge; own Forge loop and Docker tools; HTTP provider not live tested.",
        "limitations": [
            "Follow-ups received explicit review feedback and are not independent blind trials.",
            "Memory-retry was not a controlled ablation; no causal quality improvement is claimed.",
            "The final task was explicitly resumed after API deployment interruption; pre-resume usage is unknown.",
            "Reported model usage covers completed reported calls, not unreported interrupted calls or billing.",
            "100 queue admissions and unique leases do not establish 100 simultaneous model executions.",
        ],
        "initial_submission_snapshots": initial,
        "cachetools_followups": attempts,
        "final_task_pre_resume_failure": compact_task(read("forge-cachetools-test-repair-pre-resume.json")),
        "reports": {name: {"sha256": digest(RESULTS / name), "data": read(name)} for name in report_names},
        "ui": {"asset_source": ui["asset_source"], "passed": ui["passed"],
               "checks": [{"name": row["name"], "passed": row["passed"]} for row in ui["checks"]],
               "raw_report_sha256": digest(RESULTS / "studio-deployed-final/studio-check.json")},
    }
    destination = ROOT / "benchmarks" / "forge-run-summary.json"
    destination.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    patches = ROOT / "benchmarks" / "example-patches"
    patches.mkdir(exist_ok=True)
    for source, target in [(final_name + ".patch", "cachetools-reviewed.patch"),
                           ("forge-boltons-pearson.patch", "boltons-first.patch")]:
        (patches / target).write_bytes((RESULTS / source).read_bytes())
    print(json.dumps({"snapshot": str(destination.relative_to(ROOT)), "distinct_tasks": 5,
                      "final_candidate_sha256": final_hash}))


if __name__ == "__main__":
    main()

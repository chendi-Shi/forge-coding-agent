#!/usr/bin/env python3
"""Run a separate post-hoc cachetools compatibility review in offline Docker.

This does not change the frozen hidden oracle or its original scores. Each run
checks the frozen parent, a hash-verified upstream source fix, and the supplied
candidate against the same three review checks. After feedback, these checks
are known regression tests rather than an independent holdout.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from prepare_issue_benchmarks import (
    BENCHMARKS,
    CASES,
    digest,
    docker_prefix,
    git_blob,
    pinned_image,
    read_zip,
    run_docker,
    verified_inputs,
)

CASE = "cachetools-autospec"
REVIEW_PATH = "_post_hoc_review/test_descriptor_introspection.py"


def review(candidate: Path, prefix: list[str], image: str) -> dict:
    source, original_oracle, manifest = verified_inputs(CASE)
    spec = CASES[CASE]
    case_dir = BENCHMARKS / CASE
    probe = (case_dir / "post_hoc_review/test_descriptor_introspection.py").read_bytes()
    reference = (case_dir / "oracle/reference_files" / spec["reference_file"]).read_bytes()
    if git_blob(reference) != spec["reference_blob"]:
        raise ValueError("reference file hash mismatch")
    candidate_raw = candidate.read_bytes()
    files, skipped = read_zip(candidate_raw)
    if skipped:
        raise ValueError("candidate contains excluded paths")
    if REVIEW_PATH in files:
        raise ValueError("candidate contains evaluator review path")
    for name, data in source.items():
        if (name.startswith("tests/") or PurePosixPath(name).name == "conftest.py") and files.get(name) != data:
            raise ValueError("candidate changed or removed an existing test")
    review_spec = {**spec, "test_args": [REVIEW_PATH]}
    variants = {
        "parent": source,
        "reference": {**source, spec["reference_file"]: reference},
        "candidate": files,
    }
    outcomes = {
        name: run_docker(prefix, image, review_spec, {**variant, REVIEW_PATH: probe}, None)
        for name, variant in variants.items()
    }
    controls_passed = all(outcomes[name]["exit_code"] == 0 for name in ("parent", "reference"))
    candidate_passed = outcomes["candidate"]["exit_code"] == 0
    return {
        "case": CASE,
        "review_kind": "post_hoc_compatibility_review",
        "independent_holdout": False,
        "created_at": datetime.now(UTC).isoformat(),
        "passed": controls_passed and candidate_passed,
        "controls_passed": controls_passed,
        "candidate_passed": candidate_passed,
        "engine": "docker_network_none",
        "image": image,
        "parent_sha": spec["parent"],
        "fix_sha": spec["fix"],
        "reference_scope": [spec["reference_file"]],
        "reference_git_blob_sha": spec["reference_blob"],
        "baseline_sha256": manifest["project_zip_sha256"],
        "candidate_sha256": digest(candidate_raw),
        "review_sha256": digest(probe),
        "frozen_hidden_oracle_sha256": digest(original_oracle),
        "frozen_hidden_oracle_modified": False,
        "existing_tests_unchanged": True,
        "checks": ["raw_descriptor_autospec", "iscoroutinefunction", "isgeneratorfunction"],
        "outcomes": outcomes,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--docker-prefix", help='JSON argv array, e.g. ["docker"]')
    parser.add_argument("--image", default="agent-backend-sandbox:0.1.0")
    args = parser.parse_args()
    prefix = docker_prefix(args.docker_prefix)
    image = pinned_image(prefix, args.image)
    result = review(args.candidate, prefix, image)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in result.items() if key != "outcomes"}, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Re-run the frozen synthetic retrieval regression benchmark locally.

Uses the corpus, relevance labels, and scoring function in the existing test
module without copying or deriving labels. Requires the project's dev extras
(pytest is imported by that module); no server, model, or network is used.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import runpy
import sys
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=root / "results" / "retrieval-ablation.json")
    args = parser.parse_args()

    # Load the exact checked-in fixture, including its evaluator, rather than
    # maintaining an independent benchmark that could silently change labels.
    sys.path.insert(0, str(root / "src"))
    fixture_path = root / "tests" / "test_forge_retrieval.py"
    fixture = runpy.run_path(str(fixture_path))
    files, cases = fixture["FILES"], fixture["CASES"]
    index = fixture["CodeIndex"].from_files(files, source_version=fixture["VERSION"])
    modes = (("path", "naive path"), ("lexical", "lexical"), ("hybrid", "hybrid"))
    results = [dict(fixture["evaluate"](index, mode), display_name=name) for mode, name in modes]
    labels = [
        {"query_id": case.identifier, "query": case.query, "relevant_paths": sorted(case.relevant_paths)}
        for case in cases
    ]
    frozen_inputs = json.dumps({"files": files, "queries": labels}, sort_keys=True, ensure_ascii=False)
    report = {
        "benchmark_type": "synthetic_frozen_regression",
        "limitations": [
            "This small, hand-written fixture is not a real-issue evaluation or production estimate.",
            "Equal lexical and hybrid scores do not establish a benefit from symbol-aware ranking.",
            "All modes receive identical queries, files, five-hit limits, and 1800-token budgets.",
            "Metrics deduplicate retrieved chunks into ranked files; fewer than five distinct files may remain.",
        ],
        "fixture_module": fixture_path.relative_to(root).as_posix(),
        "source_version": fixture["VERSION"],
        "frozen_inputs_sha256": hashlib.sha256(frozen_inputs.encode("utf-8")).hexdigest(),
        "retrieval_source_sha256": hashlib.sha256(
            (root / "src" / "agent_backend" / "forge" / "retrieval.py").read_bytes()
        ).hexdigest(),
        "file_count": len(files),
        "query_count": len(cases),
        "relevance_label_count": sum(len(case.relevant_paths) for case in cases),
        "rank_unit": "distinct_file",
        "aggregation": "macro_average_over_queries",
        "max_hits": 5,
        "max_tokens": 1800,
        "queries": labels,
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"Synthetic frozen regression: {len(files)} files, {len(cases)} queries; not real-issue evaluation.")
    for result in results:
        print(f"{result['display_name']}: Recall@3={result['recall_at_3']:.6f}, MRR@5={result['mrr_at_5']:.6f}")
    print(f"Report: {args.output.resolve()}")


if __name__ == "__main__":
    main()

"""Export credential-checked, portable reports from the hardening probes.

Run after the live probes. This does not execute tests or infer model quality.
The source manifest identifies bytes independently of the eventual Git commit.
"""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
REPORTS = {
    "acceptance-live": "acceptance-live.json",
    "acceptance-sandbox": "acceptance-sandbox.json",
    "acceptance-oss-live": "acceptance-oss-live.json",
    "forge-artifacts": "forge-artifacts.json",
    "forge-recovery-durable": "forge-recovery.json",
    "coding-flow-acceptance": "coding-flow.json",
    "studio-acceptance": "studio-acceptance/studio-check.json",
    "deployment-hardening": "deployment-hardening.json",
}


def main():
    settings = dotenv_values(ROOT / ".env")
    secrets = list(json.loads(settings.get("API_KEYS_JSON") or "{}").values())
    secrets.extend(settings.get(name) for name in ("BROKER_TOKEN", "POSTGRES_PASSWORD", "FORGE_API_KEY"))
    secrets = [value for value in secrets if isinstance(value, str) and len(value) >= 8]
    outputs = {}
    for name, source in REPORTS.items():
        value = json.loads((ROOT / "results" / source).read_text(encoding="utf-8"))
        success = (all(result is True for key, result in value.items() if key != "real_model_calls")
                   if name == "coding-flow-acceptance" else value.get("ok", value.get("all_passed", value.get("passed"))))
        if name == "deployment-hardening":
            success = len(value["api_replicas"]) == 2 and all(replica["healthy"] and replica["source_files_matched"] > 0 for replica in value["api_replicas"])
        if success is not True:
            raise ValueError(f"Probe has not passed: {name}")
        if name == "studio-acceptance":
            # Remove absolute screenshot paths; the live report remains local.
            value["screenshots"] = [Path(path).name for path in value["screenshots"]]
        serialized = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        if any(secret in serialized for secret in secrets):
            raise ValueError(f"Credential found; export refused: {name}")
        outputs[name + ".json"] = serialized.encode("utf-8")

    code = [path for path in (ROOT / "src").rglob("*") if path.suffix in {".py", ".js", ".html", ".css"}]
    code += list((ROOT / "scripts").glob("*.py")) + list((ROOT / "scripts").glob("*.cjs"))
    code += list((ROOT / "tests").glob("*.py"))
    code += [ROOT / name for name in ("Dockerfile", "deploy/sandbox.Dockerfile", "compose.yaml", "requirements.lock", "pyproject.toml", ".github/workflows/checks.yml")]
    manifest = {
        "exported_at": datetime.now(UTC).isoformat(), "new_model_calls": 0,
        "scope": "Deterministic engineering probes and regression grading of previously generated model patches; not new model success rates.",
        "source_sha256": {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(code)},
        "report_sha256": {name: hashlib.sha256(data).hexdigest() for name, data in outputs.items()},
    }
    destination = ROOT / "docs/evidence"
    destination.mkdir(parents=True, exist_ok=True)
    for name, data in outputs.items():
        (destination / name).write_bytes(data)
    (destination / "engineering-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"exported_reports": len(outputs), "source_files": len(code), "credentials_found": False}))


if __name__ == "__main__":
    main()

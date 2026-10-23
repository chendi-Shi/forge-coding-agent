#!/usr/bin/env python3
"""Freeze real upstream bugs and run hidden regression oracles in offline Docker.

Only project.zip and task.md are Agent inputs. oracle/, provenance.json and
reference artifacts remain evaluator-side; do not import the whole case folder.
"""

from __future__ import annotations

import argparse
import base64
import difflib
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from uuid import uuid4

PROJECT = Path(__file__).resolve().parents[1]
BENCHMARKS = PROJECT / "benchmarks"
MAX_FILE = 2 * 1024 * 1024
MAX_TOTAL = 20 * 1024 * 1024
MAX_ZIP = 16 * 1024 * 1024
MAX_FILES = 2000
CASES = {
    "cachetools-autospec": {
        "repo": "tkem/cachetools",
        "parent": "eba200635273fe45b67e669b84cb45f995b27c24",
        "fix": "95e6addbd82afd75f81cb5650f32f6d74af2f1f9",
        "reference_file": "src/cachetools/_descriptor.py",
        "reference_blob": "596483aaf6753e5e3178ce8340f0f60db03260bc",
        "license_blob": "f980a19c78c25959f922b8cc93854d91a89e686f",
        "test_args": ["tests/test_cachedmethod.py"],
        "python_path": "src",
        "expected_baseline_error": "TypeError",
    },
    "boltons-pearson": {
        "repo": "mahmoud/boltons",
        "parent": "3913220988e1e7c80620796fdcb4f74eb598d179",
        "fix": "73e22c90de25aaae3e6aed57f7438ffe57349339",
        "reference_file": "boltons/statsutils.py",
        "reference_blob": "abf5e09a5ccc68e53b1bdd78b153a7d7c4e64f1f",
        "license_blob": "6a4c9d2496f53c61c99ae7c113e6ed94b15b038c",
        "test_args": ["tests/test_statsutils.py"],
        "python_path": ".",
        "expected_baseline_error": "ZeroDivisionError",
    },
}
EXCLUDE = {".git", ".github", ".gitlab", ".circleci", ".codex", ".agents", ".ssh", ".aws",
           "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache",
           "build", "dist", "oracle", "_hidden_oracle", "_downloads"}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git_blob(data: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


def safe_name(name: str) -> bool:
    path = PurePosixPath(name)
    return bool(name) and str(path) == name and not path.is_absolute() and not any(
        part in {"", ".", ".."} or ":" in part or "\\" in part or "\x00" in part for part in path.parts
    )


def included(name: str) -> bool:
    path = PurePosixPath(name)
    return (safe_name(name) and not any(part in EXCLUDE or part.startswith(".") for part in path.parts)
            and path.name.upper() not in {"AGENTS.MD", "CLAUDE.MD"}
            and not any(part.lower().endswith((".pem", ".key", ".p12", ".pfx")) for part in path.parts))


def read_zip(data: bytes, *, strip_root: bool = False) -> tuple[dict[str, bytes], list[str]]:
    if len(data) > MAX_ZIP:
        raise ValueError("archive exceeds 16 MiB")
    result = {}
    skipped = []
    total = 0
    roots = set()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for item in archive.infolist():
            if not safe_name(item.filename.rstrip("/")):
                raise ValueError("unsafe archive path")
            mode = item.external_attr >> 16
            if stat.S_ISLNK(mode) or item.flag_bits & 1:
                raise ValueError("archive contains symlink or encrypted member")
            if item.is_dir():
                continue
            parts = PurePosixPath(item.filename).parts
            roots.add(parts[0])
            name = "/".join(parts[1:]) if strip_root else item.filename
            if not included(name):
                skipped.append(name)
                continue
            if name in result or item.file_size > MAX_FILE:
                raise ValueError("duplicate archive path or oversized file")
            total += item.file_size
            if total > MAX_TOTAL or len(result) >= MAX_FILES:
                raise ValueError("expanded archive exceeds project limit")
            result[name] = archive.read(item)
    if strip_root and len(roots) != 1:
        raise ValueError("upstream archive has more than one root")
    if not result:
        raise ValueError("empty project archive")
    return result, sorted(skipped)


def make_zip(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
    result = output.getvalue()
    read_zip(result)
    return result


def download(url: str, target: Path) -> bytes:
    if target.exists():
        return target.read_bytes()
    # Public, fixed GitHub URLs only. No GitHub/API authentication is used.
    if not url.startswith(("https://codeload.github.com/", "https://raw.githubusercontent.com/")):
        raise ValueError("unexpected download host")
    request = urllib.request.Request(url, headers={"User-Agent": "forge-reproducible-benchmark/1.0"})
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read(MAX_ZIP + 1)
    if len(data) > MAX_ZIP:
        raise ValueError("download exceeds limit")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return data


def prepare(case_id: str) -> dict:
    spec = CASES[case_id]
    case = BENCHMARKS / case_id
    case.mkdir(parents=True, exist_ok=True)
    provenance_path = case / "provenance.json"
    if not provenance_path.exists() or not (case / "oracle/test_regression.py").exists():
        raise ValueError(f"{case_id}: missing frozen provenance or hidden oracle")
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    # Upstream parent relationship was independently verified through GitHub.
    if provenance.get("parent_sha") != spec["parent"] or provenance.get("fix_sha") != spec["fix"]:
        raise ValueError(f"{case_id}: provenance does not match fixed revision")
    source_url = f"https://codeload.github.com/{spec['repo']}/zip/{spec['parent']}"
    archive = download(source_url, BENCHMARKS / "_downloads" / f"{case_id}-{spec['parent']}.zip")
    files, skipped = read_zip(archive, strip_root=True)
    if git_blob(files["LICENSE"]) != spec["license_blob"]:
        raise ValueError("upstream license differs from GitHub connector evidence")
    (case / "LICENSE").write_bytes(files["LICENSE"])
    source_path = spec["reference_file"]
    fixed = download(f"https://raw.githubusercontent.com/{spec['repo']}/{spec['fix']}/{source_path}",
                     case / "oracle/reference_files" / source_path)
    if git_blob(fixed) != spec["reference_blob"]:
        raise ValueError("reference file differs from frozen GitHub blob")
    patch = "".join(difflib.unified_diff(files[source_path].decode().splitlines(True),
                                       fixed.decode().splitlines(True), fromfile="a/" + source_path,
                                       tofile="b/" + source_path))
    (case / "oracle/reference.patch").write_text(patch, encoding="utf-8")
    project_zip = make_zip(files)
    (case / "project.zip").write_bytes(project_zip)
    manifest = {
        "case": case_id, "source_archive_url": source_url, "parent_sha": spec["parent"],
        "source_archive_sha256": digest(archive), "project_zip_sha256": digest(project_zip),
        "project_file_count": len(files), "project_expanded_bytes": sum(map(len, files.values())),
        "excluded_paths": skipped,
        "files": {name: digest(data) for name, data in sorted(files.items())},
        "hidden_oracle_sha256": digest((case / "oracle/test_regression.py").read_bytes()),
        "task_description_sha256": digest((case / "task.md").read_bytes()),
        "reference_source_git_blob": spec["reference_blob"],
        "visibility": {"agent": ["project.zip", "task.md"],
                       "evaluator_only": ["oracle/", "provenance.json", "source_manifest.json", "verification.json"]},
    }
    (case / "source_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return {"case": case_id, "files": len(files), "zip_bytes": len(project_zip),
            "project_zip_sha256": digest(project_zip)}


RUNNER = r'''
import base64,json,os,pathlib,subprocess,sys
payload=json.load(sys.stdin)
root=pathlib.Path('/workspace')
for name,encoded in payload['files'].items():
    relative=pathlib.PurePosixPath(name)
    if relative.is_absolute() or '..' in relative.parts or '\\' in name or ':' in name:
        raise ValueError('unsafe payload path')
    target=root.joinpath(*relative.parts)
    target.parent.mkdir(parents=True,exist_ok=True)
    target.write_bytes(base64.b64decode(encoded,validate=True))
tests=list(payload['test_args'])
if payload.get('oracle'):
    hidden=root/'_hidden_oracle'
    hidden.mkdir()
    (hidden/'test_forge_regression.py').write_bytes(base64.b64decode(payload['oracle'],validate=True))
    tests.append('_hidden_oracle/test_forge_regression.py')
env=dict(os.environ, PYTHONPATH=str(root/payload['python_path']), PYTEST_DISABLE_PLUGIN_AUTOLOAD='1')
command=[sys.executable,'-m','pytest','-q','--noconftest','-c','/dev/null','-p','no:cacheprovider',*tests]
try:
    result=subprocess.run(command,cwd=root,env=env,capture_output=True,text=True,timeout=45)
    report={'exit_code':result.returncode,'stdout':result.stdout[-131072:],'stderr':result.stderr[-32768:]}
except subprocess.TimeoutExpired:
    report={'exit_code':124,'stdout':'','stderr':'pytest timeout'}
print(json.dumps(report))
'''


def docker_prefix(value: str | None) -> list[str]:
    if value:
        result = json.loads(value)
        if not isinstance(result, list) or not result or not all(isinstance(item, str) and item for item in result):
            raise ValueError("docker-prefix must be a JSON argv array")
        return result
    runtime_file = PROJECT / ".runtime.json"
    if os.name == "nt" and runtime_file.exists():
        runtime = json.loads(runtime_file.read_text(encoding="utf-8-sig"))
        if runtime.get("backend") == "wsl":
            distro = runtime.get("distro", "")
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", distro):
                raise ValueError("invalid runtime distribution")
            return ["wsl.exe", "-d", distro, "-u", "root", "--", "/usr/bin/docker", "--host", "unix:///var/run/docker.sock"]
    return ["docker"]


def pinned_image(prefix: list[str], image: str) -> str:
    result = subprocess.run([*prefix, "image", "inspect", "--format", "{{.Id}}", image],
                            capture_output=True, text=True, timeout=15)
    identifier = result.stdout.strip()
    if result.returncode or not re.fullmatch(r"sha256:[0-9a-f]{64}", identifier):
        raise RuntimeError("benchmark image is not available locally")
    return identifier


def run_docker(prefix: list[str], image: str, spec: dict, files: dict[str, bytes], oracle: bytes | None) -> dict:
    run_id = uuid4().hex
    name = "forge-benchmark-" + run_id
    payload = {"files": {key: base64.b64encode(value).decode() for key, value in files.items()},
               "oracle": base64.b64encode(oracle).decode() if oracle is not None else None,
               "test_args": spec["test_args"], "python_path": spec["python_path"]}
    # ASCII transport avoids WSL's argv quoting changing Python backslashes.
    runner_command = "import base64;exec(base64.b64decode('" + base64.b64encode(RUNNER.encode()).decode() + "'))"
    argv = [*prefix, "run", "--rm", "--pull=never", "-i", "--name", name,
            "--label", "io.forge.benchmark=" + run_id, "--network", "none", "--read-only",
            "--memory", "512m", "--cpus", "1", "--pids-limit", "64", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true", "--user", "10001:10001",
            "--tmpfs", "/workspace:rw,nosuid,nodev,size=128m,mode=1777",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=32m,mode=1777", "--entrypoint", "python", image, "-c", runner_command]
    try:
        result = subprocess.run(argv, input=json.dumps(payload).encode(), capture_output=True, timeout=75)
        if result.returncode:
            raise RuntimeError("benchmark container failed: " + result.stderr.decode("utf-8", "replace")[-2000:])
        parsed = json.loads(result.stdout)
        if not isinstance(parsed.get("exit_code"), int):
            raise ValueError("invalid benchmark runner response")
        return parsed
    except subprocess.TimeoutExpired:
        raise RuntimeError("benchmark container timed out") from None
    finally:
        # Never touch containers not created by this invocation. Inspect labels
        # before deleting a container left running by a timed-out Docker client.
        check = subprocess.run([*prefix, "inspect", "--format", '{{ index .Config.Labels "io.forge.benchmark" }}', name],
                               capture_output=True, text=True, timeout=10)
        if check.returncode == 0 and check.stdout.strip() == run_id:
            subprocess.run([*prefix, "rm", "-f", name], capture_output=True, timeout=10)


def verified_inputs(case_id: str) -> tuple[dict[str, bytes], bytes, dict]:
    case = BENCHMARKS / case_id
    manifest = json.loads((case / "source_manifest.json").read_text(encoding="utf-8"))
    archive = (case / "project.zip").read_bytes()
    oracle = (case / "oracle/test_regression.py").read_bytes()
    if digest(archive) != manifest["project_zip_sha256"] or digest(oracle) != manifest["hidden_oracle_sha256"]:
        raise ValueError("frozen source or hidden oracle changed; rerun prepare deliberately")
    if digest((case / "task.md").read_bytes()) != manifest["task_description_sha256"]:
        raise ValueError("frozen task description changed; rerun prepare deliberately")
    files, skipped = read_zip(archive)
    if skipped or {name: digest(data) for name, data in files.items()} != manifest["files"]:
        raise ValueError("project does not match frozen manifest")
    return files, oracle, manifest


def verify(case_id: str, prefix: list[str], image: str) -> dict:
    case = BENCHMARKS / case_id
    spec = CASES[case_id]
    files, oracle, manifest = verified_inputs(case_id)
    reference = (case / "oracle/reference_files" / spec["reference_file"]).read_bytes()
    if git_blob(reference) != spec["reference_blob"]:
        raise ValueError("reference file hash mismatch")
    public = run_docker(prefix, image, spec, files, None)
    baseline = run_docker(prefix, image, spec, files, oracle)
    fixed_files = {**files, spec["reference_file"]: reference}
    fixed = run_docker(prefix, image, spec, fixed_files, oracle)
    passed = (public["exit_code"] == 0 and baseline["exit_code"] == 1
              and spec["expected_baseline_error"] in baseline["stdout"] and fixed["exit_code"] == 0)
    report = {"case": case_id, "passed": passed, "engine": "docker_network_none", "image": image,
              "parent_sha": spec["parent"], "fix_sha": spec["fix"],
              "source_sha256": manifest["project_zip_sha256"],
              "public_baseline": public, "baseline_fail": baseline, "reference_pass": fixed,
              "reference_scope": [spec["reference_file"]], "agent_model_evaluation": False}
    (case / "verification.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def evaluate(case_id: str, candidate: Path, prefix: list[str], image: str) -> dict:
    source, oracle, manifest = verified_inputs(case_id)
    raw = candidate.read_bytes()
    files, skipped = read_zip(raw)
    if skipped:
        raise ValueError("candidate contains excluded paths")
    # Existing upstream tests are protected; candidates may add their own tests.
    for name, data in source.items():
        if (name.startswith("tests/") or PurePosixPath(name).name == "conftest.py") and files.get(name) != data:
            raise ValueError("candidate changed or removed an existing test")
    added_tests = sorted(name for name in files.keys() - source.keys()
                         if name.startswith("tests/") and PurePosixPath(name).name.startswith("test_")
                         and name.endswith(".py"))
    spec = {**CASES[case_id], "test_args": [*CASES[case_id]["test_args"], *added_tests]}
    outcome = run_docker(prefix, image, spec, files, oracle)
    return {"case": case_id, "passed": outcome["exit_code"] == 0, "engine": "docker_network_none",
            "candidate_sha256": digest(raw), "baseline_sha256": manifest["project_zip_sha256"],
            "existing_tests_unchanged": True, "added_tests_executed": added_tests, "hidden_oracle": outcome}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "verify", "evaluate"])
    parser.add_argument("--case", choices=sorted(CASES))
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--docker-prefix", help="JSON array, e.g. [\"docker\"]")
    parser.add_argument("--image", default="agent-backend-sandbox:0.1.0")
    args = parser.parse_args()
    selected = [args.case] if args.case else list(CASES)
    if args.action == "evaluate" and (not args.case or not args.candidate):
        parser.error("evaluate requires --case and --candidate")
    if args.action == "prepare":
        results = [prepare(case_id) for case_id in selected]
    else:
        prefix = docker_prefix(args.docker_prefix)
        image = pinned_image(prefix, args.image)
        results = ([verify(case_id, prefix, image) for case_id in selected] if args.action == "verify"
                   else [evaluate(args.case, args.candidate, prefix, image)])
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    # Full test logs remain in the report, not the normal console summary.
    print(json.dumps([{key: value for key, value in result.items()
                       if key not in {"public_baseline", "baseline_fail", "reference_pass", "hidden_oracle"}}
                      for result in results], indent=2))
    return 0 if all(result.get("passed", True) for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

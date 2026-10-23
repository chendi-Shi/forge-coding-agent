"""Fixed pytest acceptance runner; the suite is mounted read-only outside the candidate.

This prevents repository configuration and stdout from declaring success. Python
under test still shares the pytest process; this is not an adversarial interpreter.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from importlib.machinery import PathFinder
from pathlib import Path, PurePosixPath

COUNTERS = ("collected", "passed", "failed", "errors", "skipped", "xfailed", "xpassed")
MAX_SUITE_BYTES = 2 * 1024 * 1024


def relative_path(value, *, directory=False):
    if not isinstance(value, str) or not value or len(value) > 500 or "\\" in value or "\x00" in value:
        raise ValueError("Invalid acceptance path")
    path = PurePosixPath(value)
    if path.is_absolute() or ":" in value or any(part in {"..", ".git", ".env", ".codex"} or part.startswith("-") for part in path.parts):
        raise ValueError("Acceptance paths must be inside their assigned directory")
    if str(path) != value or (value == "." and not directory):
        raise ValueError("Acceptance paths must be canonical")
    return value


def validate_options(arguments):
    if not isinstance(arguments, dict) or set(arguments) != {"test_paths", "python_paths", "minimum_tests", "timeout_seconds"}:
        raise ValueError("Invalid acceptance options")
    for name in ("test_paths", "python_paths"):
        values = arguments[name]
        if not isinstance(values, list) or not 1 <= len(values) <= 200:
            raise ValueError("Acceptance paths must be nonempty lists")
        for value in values:
            relative_path(value, directory=name == "python_paths")
        if len(set(values)) != len(values):
            raise ValueError("Duplicate acceptance path")
    for value in arguments["test_paths"]:
        path = PurePosixPath(value)
        if path.suffix != ".py" or not (path.name.startswith("test_") or path.name.endswith("_test.py")):
            raise ValueError("Explicit Python test files are required")
    for name, maximum in (("minimum_tests", 10000), ("timeout_seconds", 120)):
        value = arguments[name]
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
            raise ValueError("Invalid acceptance execution limit")


def validate_verify(arguments):
    if not isinstance(arguments, dict) or "files" not in arguments:
        raise ValueError("Acceptance files are required")
    options = {key: value for key, value in arguments.items() if key != "files"}
    validate_options(options)
    files = arguments["files"]
    if not isinstance(files, dict) or not 1 <= len(files) <= 200:
        raise ValueError("Acceptance suite must contain 1 to 200 files")
    total = 0
    for name, content in files.items():
        relative_path(name)
        path = PurePosixPath(name)
        if path.suffix != ".py" or path.name.casefold() in {"conftest.py", "pytest.py"}:
            raise ValueError("Only Python suite files without conftest.py or pytest.py are allowed")
        if not isinstance(content, str):
            raise ValueError("Acceptance file content must be text")
        size = len(content.encode("utf-8"))
        total += size
        if size > 256 * 1024 or total > MAX_SUITE_BYTES:
            raise ValueError("Acceptance suite exceeds its size limit")
    if any(path not in files for path in options["test_paths"]):
        raise ValueError("Every selected acceptance test must exist in the suite")
    for path in files:
        if any(str(parent) in files for parent in PurePosixPath(path).parents if str(parent) != "."):
            raise ValueError("Acceptance file/directory conflict")
    return options


def _inside(root, value):
    path = root
    for part in PurePosixPath(value).parts:
        path /= part
        if path.is_symlink():
            raise ValueError("Acceptance import and test paths cannot be symlinks")
    if not path.exists():
        raise ValueError("Acceptance import or test path does not exist")
    return path


def _import_collisions(directories):
    # These files are intentionally ignored interference, never supported source
    # targets. Packages with the same names are still checked for collisions.
    ignored_files = {"pytest.py", "conftest.py", "sitecustomize.py", "usercustomize.py", "__init__.py"}
    collisions = set()
    for directory in directories:
        for entry in directory.iterdir():
            if entry.name.startswith(".") or entry.name == "__pycache__":
                continue
            if entry.is_file() and entry.suffix == ".py" and entry.name not in ignored_files:
                name = entry.stem
            elif entry.is_dir() and any(entry.rglob("*.py")):
                name = entry.name
            else:
                continue
            if name in sys.stdlib_module_names or name in sys.modules or PathFinder.find_spec(name) is not None:
                collisions.add(name)
    return sorted(collisions)


class Statistics:
    def __init__(self):
        self.tests = dict.fromkeys(COUNTERS, 0)
        self.deselected = 0

    def pytest_collection_finish(self, session):
        self.tests["collected"] = len(session.items)

    def pytest_deselected(self, items):
        self.deselected += len(items)

    def pytest_collectreport(self, report):
        if report.failed:
            self.tests["errors"] += 1
        elif report.skipped:
            self.tests["skipped"] += 1

    def pytest_runtest_logreport(self, report):
        if hasattr(report, "wasxfail"):
            self.tests["xfailed" if report.skipped else "xpassed"] += 1
        elif report.failed:
            self.tests["failed" if report.when == "call" else "errors"] += 1
        elif report.skipped:
            self.tests["skipped"] += 1
        elif report.when == "call" and report.passed:
            self.tests["passed"] += 1


def _run_pytest(arguments, *, workspace=Path("/workspace"), acceptance=Path("/acceptance"), config=Path("/opt/sandbox/acceptance.ini")):
    validate_options(arguments)
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    os.environ.pop("PYTEST_ADDOPTS", None)
    os.environ.pop("PYTEST_PLUGINS", None)
    # Import pytest from the image before exposing candidate import paths.
    import pytest

    directories = [_inside(workspace, path) for path in arguments["python_paths"]]
    tests = [_inside(acceptance, path) for path in arguments["test_paths"]]
    if any(not path.is_dir() for path in directories) or any(not path.is_file() for path in tests):
        raise ValueError("Invalid acceptance file or import directory")
    if collisions := _import_collisions(directories):
        return {"pytest_exit_code": 2, "tests": dict.fromkeys(COUNTERS, 0), "passed": False,
                "unsupported_reason": "import_collision:" + ",".join(collisions)[:400]}
    sys.path.extend([str(acceptance), *(str(path) for path in directories)])
    os.chdir(acceptance)
    stats = Statistics()
    code = int(pytest.main([
        "-q", "--tb=no", "--show-capture=no", "-rN", "-c", str(config), "--rootdir", str(acceptance), "--noconftest",
        "--import-mode=importlib", "-p", "no:cacheprovider", *map(str, tests),
    ], plugins=[stats]))
    passed = (code == 0 and stats.tests["collected"] >= arguments["minimum_tests"]
              and stats.tests["passed"] == stats.tests["collected"]
              and not stats.deselected and not any(stats.tests[name] for name in COUNTERS[2:]))
    return {"pytest_exit_code": code, "tests": stats.tests, "passed": passed}


def run_acceptance(arguments):
    validate_options(arguments)
    try:
        from .runner import run_command
    except ImportError:
        from runner import run_command

    with tempfile.TemporaryDirectory(prefix="acceptance-result-") as directory:
        report = Path(directory) / "result.json"
        result = run_command({
            "argv": [sys.executable, "-I", str(Path(__file__).resolve()), "--child", json.dumps(arguments), str(report)],
            "timeout_seconds": arguments["timeout_seconds"], "max_output_bytes": 32000,
        })
        result.update(tests=dict.fromkeys(COUNTERS, 0), passed=False)
        if not result["timed_out"] and not report.is_symlink() and report.is_file() and report.stat().st_size <= 16384:
            try:
                stored = json.loads(report.read_text(encoding="utf-8"))
                valid = (isinstance(stored, dict) and set(stored.get("tests", {})) == set(COUNTERS)
                         and all(type(value) is int and value >= 0 for value in stored["tests"].values()))
                if valid:
                    result["tests"] = stored["tests"]
                    if isinstance(stored.get("unsupported_reason"), str):
                        result["unsupported_reason"] = stored["unsupported_reason"][:500]
                    counts = stored["tests"]
                    result["passed"] = (stored.get("passed") is True and result["exit_code"] == 0
                                        and stored.get("pytest_exit_code") == 0
                                        and counts["collected"] >= arguments["minimum_tests"]
                                        and counts["passed"] == counts["collected"]
                                        and not any(counts[name] for name in COUNTERS[2:]))
            except (ValueError, TypeError, OSError):
                pass
        return result


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] != "--child":
        raise SystemExit(2)
    outcome = _run_pytest(json.loads(sys.argv[2]))
    Path(sys.argv[3]).write_text(json.dumps(outcome), encoding="utf-8")
    raise SystemExit(0 if outcome["passed"] else (outcome["pytest_exit_code"] or 1))

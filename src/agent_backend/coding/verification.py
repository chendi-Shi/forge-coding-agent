"""Versioned, private acceptance policies and fail-closed verification decisions.

These checks protect evaluator files and bind a result to exact candidate bytes.
They do not make Python importing hostile candidate code a secure interpreter.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import stat
import zipfile
from collections.abc import Mapping
from datetime import datetime
from pathlib import PurePosixPath
from uuid import uuid4

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from agent_backend.coding.broker import broker
from agent_backend.coding.models import CodingBase, new_id, now

MAX_ARCHIVE = 16 * 1024 * 1024
MAX_FILE = 2 * 1024 * 1024
MAX_TOTAL = 20 * 1024 * 1024
MAX_FILES = 2000
MAX_SUITE_FILE = 256 * 1024
MAX_SUITE_TOTAL = 1024 * 1024
SHA256 = re.compile(r"^[0-9a-f]{64}$")
RESERVED = {".git", ".codex", ".sandbox", ".sandbox-internal"}
COUNTERS = ("collected", "passed", "failed", "errors", "skipped", "xfailed", "xpassed")


class VerificationError(ValueError):
    def __init__(self, message: str, code: str = "INVALID_ACCEPTANCE_POLICY"):
        super().__init__(message)
        self.code = code


class AcceptanceSuiteCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(default="Acceptance suite", min_length=1, max_length=120)
    files: dict[str, str] = Field(min_length=1, max_length=64)
    protected_paths: list[str] = Field(default_factory=list, max_length=1000)
    test_paths: list[str] = Field(min_length=1, max_length=64)
    python_paths: list[str] = Field(default_factory=lambda: ["."], min_length=1, max_length=16)
    minimum_tests: int = Field(default=1, ge=1, le=10000)
    timeout_seconds: int = Field(default=120, ge=1, le=120)


class AcceptanceSuite(CodingBase):
    __tablename__ = "coding_acceptance_suites"
    __table_args__ = (UniqueConstraint("project_id", "version"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String(36), index=True)
    owner: Mapped[str] = mapped_column(Text, index=True)
    version: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(Text)
    policy: Mapped[dict] = mapped_column(JSONB)
    digest: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


def _path(value: str, *, root: bool = False) -> str:
    if root and value == ".":
        return value
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 2048:
        raise VerificationError("Invalid relative path", "UNSAFE_PATH")
    if any(c in value for c in ("\\", ":", "\x00")) or value.startswith("/"):
        raise VerificationError("Only relative POSIX paths are accepted", "UNSAFE_PATH")
    for part in value.split("/"):
        lowered = part.casefold()
        if (
            part in ("", ".", "..") or part.endswith((".", " "))
            or len(part.encode("utf-8")) > 255 or any(ord(char) < 32 for char in part)
            or lowered in RESERVED or lowered == ".env" or lowered.startswith(".env.")
        ):
            raise VerificationError("Unsafe or reserved path", "UNSAFE_PATH")
    return value


def _paths(values: list[str], *, root: bool = False) -> list[str]:
    result = [_path(value, root=root) for value in values]
    if len({value.casefold() for value in result}) != len(result):
        raise VerificationError("Duplicate or ambiguous paths", "UNSAFE_PATH")
    return sorted(result)


def _validate_file_names(names) -> list[str]:
    result = _paths(list(names))
    folded = {name.casefold() for name in result}
    for name in folded:
        if any(str(parent) in folded for parent in PurePosixPath(name).parents if str(parent) != "."):
            raise VerificationError("A file cannot also be a directory", "UNSAFE_PATH")
    return result


def archive_files(data: bytes) -> dict[str, bytes]:
    """Read a bounded ZIP without extracting paths or trusting its metadata."""
    if not isinstance(data, bytes) or len(data) > MAX_ARCHIVE:
        raise VerificationError("Candidate archive exceeds its limit", "INVALID_ARCHIVE")
    result: dict[str, bytes] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_FILES * 2:
                raise VerificationError("Too many archive entries", "INVALID_ARCHIVE")
            total = 0
            seen = set()
            for info in infos:
                if info.orig_filename != info.filename:
                    raise VerificationError("Ambiguous archive filename", "INVALID_ARCHIVE")
                path = _path(info.filename.rstrip("/") if info.is_dir() else info.filename)
                if path.casefold() in seen:
                    raise VerificationError("Duplicate archive entries", "INVALID_ARCHIVE")
                seen.add(path.casefold())
                mode = stat.S_IFMT(info.external_attr >> 16)
                if mode not in (0, stat.S_IFREG, stat.S_IFDIR) or info.flag_bits & 1:
                    raise VerificationError("Links, devices and encrypted entries are forbidden", "INVALID_ARCHIVE")
                if info.is_dir():
                    if mode == stat.S_IFREG:
                        raise VerificationError("Invalid directory metadata", "INVALID_ARCHIVE")
                    continue
                if mode == stat.S_IFDIR or info.file_size > MAX_FILE:
                    raise VerificationError("Invalid or oversized archive file", "INVALID_ARCHIVE")
                total += info.file_size
                if total > MAX_TOTAL or len(result) >= MAX_FILES:
                    raise VerificationError("Archive expansion exceeds its limit", "INVALID_ARCHIVE")
                with archive.open(info) as stream:
                    value = stream.read(MAX_FILE + 1)
                if len(value) != info.file_size or len(value) > MAX_FILE:
                    raise VerificationError("Archive file size is inconsistent", "INVALID_ARCHIVE")
                result[path] = value
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError) as error:
        raise VerificationError("Candidate archive is not a valid supported ZIP", "INVALID_ARCHIVE") from error
    _validate_file_names(result)
    return result


def _json_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def content_digest(files: Mapping[str, bytes]) -> str:
    """Canonical content hash, independent of ZIP order, timestamps or compression."""
    records = []
    for path in _validate_file_names(files):
        content = files[path]
        if not isinstance(content, bytes):
            raise VerificationError("File contents must be bytes")
        records.append({"path": path, "sha256": hashlib.sha256(content).hexdigest(), "size": len(content)})
    return hashlib.sha256(_json_bytes(records)).hexdigest()


def _manifest(value: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(value, dict) or len(value) > 1000:
        raise VerificationError("Invalid protected manifest")
    paths = _validate_file_names(value)
    if any(not isinstance(value[path], str) or not SHA256.fullmatch(value[path]) for path in paths):
        raise VerificationError("Invalid protected file digest")
    return {path: value[path] for path in paths}


def compile_suite(spec: AcceptanceSuiteCreate | dict, baseline_files: Mapping[str, bytes]) -> dict:
    if not isinstance(spec, AcceptanceSuiteCreate):
        spec = AcceptanceSuiteCreate.model_validate(spec)
    _validate_file_names(baseline_files)
    protected = _paths(spec.protected_paths)
    if any(path not in baseline_files for path in protected):
        raise VerificationError("A protected path is missing from the project baseline", "PROTECTED_PATH_MISSING")
    manifest = {path: hashlib.sha256(baseline_files[path]).hexdigest() for path in protected}
    return _compile_policy(spec, manifest)


def _compile_policy(spec: AcceptanceSuiteCreate, manifest: dict) -> dict:
    names = _validate_file_names(spec.files)
    python_paths = _paths(spec.python_paths, root=True)
    if any(part.startswith("-") for path in [*names, *python_paths] for part in path.split("/")):
        raise VerificationError("Acceptance and Python import paths cannot begin with options")
    total = 0
    for path in names:
        if not path.endswith(".py") or PurePosixPath(path).name.casefold() in {"conftest.py", "pytest.py"}:
            raise VerificationError("Acceptance files must be Python files, excluding conftest.py and pytest.py")
        size = len(spec.files[path].encode("utf-8"))
        total += size
        if size > MAX_SUITE_FILE or total > MAX_SUITE_TOTAL:
            raise VerificationError("Acceptance suite exceeds its content limit")
    tests = _paths(spec.test_paths)
    for path in tests:
        name = PurePosixPath(path).name
        if path not in spec.files or not (name.startswith("test_") or name.endswith("_test.py")):
            raise VerificationError("Each selected test must be an existing test_*.py or *_test.py file")
    policy = {
        "version": 1, "runner": "pytest-v1", "files": {path: spec.files[path] for path in names},
        "test_paths": tests, "python_paths": python_paths,
        "protected_manifest": _manifest(manifest), "minimum_tests": spec.minimum_tests,
        "timeout_seconds": spec.timeout_seconds,
    }
    policy["digest"] = hashlib.sha256(_json_bytes(policy)).hexdigest()
    return policy


def validate_suite(policy: dict) -> dict:
    try:
        if not isinstance(policy, dict) or set(policy) != {
            "version", "runner", "files", "test_paths", "python_paths", "protected_manifest",
            "minimum_tests", "timeout_seconds", "digest",
        } or type(policy["version"]) is not int or policy["version"] != 1 or policy["runner"] != "pytest-v1":
            raise VerificationError("Unsupported acceptance policy")
        manifest = _manifest(policy["protected_manifest"])
        spec = AcceptanceSuiteCreate(
            files=policy["files"], test_paths=policy["test_paths"], python_paths=policy["python_paths"],
            protected_paths=list(manifest), minimum_tests=policy["minimum_tests"],
            timeout_seconds=policy["timeout_seconds"],
        )
        canonical = _compile_policy(spec, manifest)
        if canonical != policy:
            raise VerificationError("Acceptance policy digest does not match", "POLICY_DIGEST_MISMATCH")
        return canonical
    except (KeyError, TypeError, ValueError) as error:
        if isinstance(error, VerificationError):
            raise
        raise VerificationError("Invalid acceptance policy") from error


def check_protected_files(candidate_files: Mapping[str, bytes], policy: dict) -> dict:
    policy = validate_suite(policy)
    _validate_file_names(candidate_files)
    violations = []
    for path, expected in policy["protected_manifest"].items():
        if path not in candidate_files:
            violations.append({"path": path, "reason": "missing"})
        elif hashlib.sha256(candidate_files[path]).hexdigest() != expected:
            violations.append({"path": path, "reason": "modified"})
    return {"ok": not violations, "code": "OK" if not violations else "PROTECTED_FILES_CHANGED", "violations": violations}


def tests_passed(tests: dict, minimum_tests: int = 1) -> bool:
    return (
        type(minimum_tests) is int and minimum_tests >= 1
        and isinstance(tests, dict) and all(type(tests.get(key)) is int and tests[key] >= 0 for key in COUNTERS)
        and tests["collected"] >= minimum_tests and tests["passed"] == tests["collected"]
        and all(tests[key] == 0 for key in COUNTERS[2:])
    )


def acceptance_gate(
    *, task_status: str, task_suite_id: str | None, active_suite_id: str | None,
    suite_digest: str | None, candidate_digest: str | None, verification: dict | None,
    minimum_tests: int = 1,
) -> dict:
    """Called under the Project/Task locks, using a freshly hashed candidate."""
    code = "OK"
    if task_status != "awaiting_review":
        code = "TASK_NOT_REVIEWABLE"
    elif not task_suite_id or not active_suite_id:
        code = "ACCEPTANCE_SUITE_REQUIRED"
    elif task_suite_id != active_suite_id:
        code = "ACCEPTANCE_POLICY_CHANGED"
    elif not isinstance(suite_digest, str) or not SHA256.fullmatch(suite_digest):
        code = "INVALID_POLICY_DIGEST"
    elif not isinstance(candidate_digest, str) or not SHA256.fullmatch(candidate_digest):
        code = "INVALID_CANDIDATE_DIGEST"
    elif not isinstance(verification, dict) or not isinstance(verification.get("acceptance"), dict):
        code = "TRUSTED_VERIFICATION_REQUIRED"
    else:
        acceptance = verification["acceptance"]
        if acceptance.get("suite_id") != task_suite_id or acceptance.get("policy_digest") != suite_digest:
            code = "VERIFICATION_POLICY_MISMATCH"
        elif acceptance.get("candidate_digest") != candidate_digest:
            code = "CANDIDATE_CHANGED"
        elif (
            type(verification.get("exit_code")) is not int or verification["exit_code"] != 0
            or verification.get("timed_out") or acceptance.get("passed") is not True
            or acceptance.get("violations") != [] or not tests_passed(acceptance.get("tests"), minimum_tests)
        ):
            code = "TRUSTED_TESTS_NOT_PASSED"
    return {"allowed": code == "OK", "code": code}


def suite_public(row: AcceptanceSuite) -> dict:
    """No test names, source, or policy payload is returned by public serializers."""
    return {
        "id": row.id, "project_id": row.project_id, "version": row.version,
        "name": row.name, "digest": row.digest,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


async def create_suite(session, project, body: AcceptanceSuiteCreate) -> AcceptanceSuite:
    """The caller must hold the Project row lock and commit the policy pin atomically."""
    source = await broker("GET", f"/workspaces/{project.workspace_id}/archive", raw=True)
    try:
        policy = compile_suite(body, archive_files(source))
    except VerificationError as error:
        raise HTTPException(422, {"code": error.code, "message": str(error)}) from error
    version = (await session.scalar(select(func.max(AcceptanceSuite.version)).where(
        AcceptanceSuite.project_id == project.id,
    ))) or 0
    row = AcceptanceSuite(
        id=str(uuid4()), project_id=project.id, owner=project.owner, version=version + 1,
        name=body.name, policy=policy, digest=policy["digest"],
    )
    session.add(row)
    await session.flush()
    return row


async def verify_suite(workspace_id: str, suite: AcceptanceSuite) -> dict:
    """Freeze candidate bytes first; tests execute only against this private snapshot."""
    try:
        policy = validate_suite(suite.policy)
        if suite.digest != policy["digest"]:
            raise VerificationError("Stored acceptance digest mismatch", "POLICY_DIGEST_MISMATCH")
    except VerificationError as error:
        raise HTTPException(409, {"code": error.code, "message": str(error)}) from error
    snapshot_id = str(uuid4())
    await broker("POST", f"/workspaces/{workspace_id}/fork", json={"workspace_id": snapshot_id})
    try:
        data = await broker("GET", f"/workspaces/{snapshot_id}/archive", raw=True)
        try:
            files = archive_files(data)
            digest = content_digest(files)
            protected = check_protected_files(files, policy)
        except VerificationError as error:
            raise HTTPException(422, {"code": error.code, "message": str(error)}) from error
        acceptance = {
            "suite_id": suite.id, "policy_digest": suite.digest, "candidate_digest": digest,
            "passed": False, "violations": protected["violations"], "tests": {},
        }
        if not protected["ok"]:
            return {"exit_code": 1, "stdout": "", "stderr": "Protected project files changed", "acceptance": acceptance}
        result = await broker("POST", f"/workspaces/{snapshot_id}/verify", json={
            key: policy[key] for key in ("files", "test_paths", "python_paths", "minimum_tests", "timeout_seconds")
        })
        if not isinstance(result, dict):
            raise HTTPException(502, "Invalid trusted verification response")
        raw_tests = result.get("tests", {})
        tests = {
            key: raw_tests[key] for key in COUNTERS
            if isinstance(raw_tests, dict) and type(raw_tests.get(key)) is int and 0 <= raw_tests[key] <= 1000000
        }
        acceptance["tests"] = tests
        acceptance["passed"] = (
            type(result.get("exit_code")) is int and result["exit_code"] == 0
            and not result.get("timed_out") and result.get("passed") is True
            and tests_passed(tests, policy["minimum_tests"])
        )
        # Pytest tracebacks, capture, assertion values and even candidate prints
        # can contain private tests. Public feedback is deliberately counts only.
        summary = ", ".join(f"{key}={tests[key]}" for key in COUNTERS if type(tests.get(key)) is int) if isinstance(tests, dict) else "invalid test report"
        return {
            "exit_code": 0 if acceptance["passed"] else 1,
            "stdout": f"Trusted acceptance: {summary}",
            "stderr": "" if acceptance["passed"] else "Trusted acceptance did not satisfy the fixed policy",
            "acceptance": acceptance,
        }
    finally:
        try:
            await broker("DELETE", f"/workspaces/{snapshot_id}")
        except HTTPException:
            logging.getLogger(__name__).warning("Acceptance snapshot cleanup failed: %s", snapshot_id)

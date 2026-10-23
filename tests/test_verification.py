import copy
import hashlib
import io
import stat
import zipfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from agent_backend.coding import verification as v


def archive(files, *, compression=zipfile.ZIP_STORED):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=compression) as result:
        for path, content in files.items():
            entry = zipfile.ZipInfo(path)
            entry.filename = path  # Preserve deliberately unsafe names even on Windows.
            entry.compress_type = compression
            result.writestr(entry, content)
    return stream.getvalue()


def specification(**kwargs):
    return v.AcceptanceSuiteCreate(**{
        "files": {"test_private.py": "def test_private():\n    assert True\n"},
        "test_paths": ["test_private.py"], "protected_paths": ["tests/test_public.py"],
        **kwargs,
    })


@pytest.fixture
def baseline():
    return {"app.py": b"value = 1\n", "tests/test_public.py": b"def test_public(): assert True\n"}


@pytest.fixture
def policy(baseline):
    return v.compile_suite(specification(), baseline)


def report(**changes):
    return {"collected": 2, "passed": 2, "failed": 0, "errors": 0, "skipped": 0, "xfailed": 0, "xpassed": 0, **changes}


def suite(policy):
    return v.AcceptanceSuite(
        id="suite-id", project_id="project-id", owner="test-owner", version=1,
        name="Unit acceptance", policy=policy, digest=policy["digest"],
    )


def gate_args(policy, baseline):
    digest = v.content_digest(baseline)
    return {
        "task_status": "awaiting_review", "task_suite_id": "suite-id", "active_suite_id": "suite-id",
        "suite_digest": policy["digest"], "candidate_digest": digest,
        "verification": {
            "exit_code": 0,
            "acceptance": {"suite_id": "suite-id", "policy_digest": policy["digest"],
                           "candidate_digest": digest, "passed": True, "violations": [], "tests": report()},
        },
    }


def test_archive_digest_ignores_zip_order_and_compression(baseline):
    other = dict(reversed(list(baseline.items())))
    assert v.content_digest(v.archive_files(archive(baseline))) == v.content_digest(v.archive_files(
        archive(other, compression=zipfile.ZIP_DEFLATED),
    ))
    assert v.content_digest({**baseline, "app.py": b"changed"}) != v.content_digest(baseline)


@pytest.mark.parametrize("name", ["../bad.py", "/bad.py", "C:/bad.py", "a\\bad.py", ".env", ".env.local", ".git/config", "a/../bad.py", "a./bad.py", "a//bad.py", "a\x01.py"])
def test_archive_rejects_unsafe_paths(name):
    with pytest.raises(v.VerificationError):
        v.archive_files(archive({name: b"x"}))


def test_archive_rejects_symlinks_duplicates_and_parent_file_collisions():
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as result:
        entry = zipfile.ZipInfo("link.py")
        entry.create_system = 3
        entry.external_attr = (stat.S_IFLNK | 0o777) << 16
        result.writestr(entry, "target")
    with pytest.raises(v.VerificationError):
        v.archive_files(stream.getvalue())
    for files in [{"Test.py": b"one", "test.py": b"two"}, {"a": b"one", "a/b.py": b"two"}]:
        with pytest.raises(v.VerificationError):
            v.archive_files(archive(files))


def test_archive_rejects_oversize_and_malformed():
    for data in [b"not a zip", archive({"large.py": b"x" * (v.MAX_FILE + 1)})]:
        with pytest.raises(v.VerificationError):
            v.archive_files(data)


def test_policy_hash_covers_tests_protection_and_runner_settings(policy, baseline):
    assert v.validate_suite(policy) == policy
    assert policy["protected_manifest"]["tests/test_public.py"] == hashlib.sha256(baseline["tests/test_public.py"]).hexdigest()
    for field, value in [
        ("files", {"test_private.py": "def test_private(): assert False"}),
        ("minimum_tests", 2), ("timeout_seconds", 10), ("python_paths", ["src"]),
        ("protected_manifest", {}),
    ]:
        modified = copy.deepcopy(policy)
        modified[field] = value
        with pytest.raises(v.VerificationError):
            v.validate_suite(modified)


@pytest.mark.parametrize("changes", [
    {"files": {"conftest.py": "x"}, "test_paths": ["conftest.py"]},
    {"files": {"pytest.py": "x"}, "test_paths": ["pytest.py"]},
    {"files": {"nested/conftest.py": "x"}},
    {"files": {"test_private.py": "x" * (v.MAX_SUITE_FILE + 1)}},
    {"test_paths": ["absent.py"]}, {"python_paths": ["../src"]},
    {"python_paths": ["-src"]}, {"files": {"-hidden/test_x.py": "def test_x(): pass"}, "test_paths": ["-hidden/test_x.py"]},
    {"python_paths": [".", "."]}, {"protected_paths": ["absent.py"]},
])
def test_policy_rejects_invalid_inputs(baseline, changes):
    with pytest.raises(v.VerificationError):
        v.compile_suite(specification(**changes), baseline)


@pytest.mark.parametrize("changes", [{"minimum_tests": 0}, {"minimum_tests": True}, {"minimum_tests": 10001}, {"timeout_seconds": 121}, {"unknown": 1}])
def test_policy_model_rejects_empty_or_coerced_limits(changes):
    with pytest.raises(ValidationError):
        specification(**changes)


def test_protected_files_are_exact_bytes_not_agent_claims(policy, baseline):
    assert v.check_protected_files({**baseline, "app.py": b"fixed"}, policy)["ok"]
    missing = {"app.py": baseline["app.py"]}
    assert v.check_protected_files(missing, policy)["violations"] == [{"path": "tests/test_public.py", "reason": "missing"}]
    changed = {**baseline, "tests/test_public.py": b"assert True # rewritten"}
    assert not v.check_protected_files(changed, policy)["ok"]


@pytest.mark.parametrize("changes", [
    {"collected": 0, "passed": 0}, {"collected": 2, "passed": 1}, {"skipped": 1},
    {"xfailed": 1}, {"xpassed": 1}, {"failed": 1}, {"errors": 1},
    {"passed": True}, {"collected": -1}, {"passed": "2"},
])
def test_test_counts_fail_closed(changes):
    assert not v.tests_passed(report(**changes))


def test_acceptance_gate_accepts_only_bound_complete_report(policy, baseline):
    args = gate_args(policy, baseline)
    assert v.acceptance_gate(**args) == {"allowed": True, "code": "OK"}
    assert not v.acceptance_gate(**{**args, "minimum_tests": 3})["allowed"]


@pytest.mark.parametrize("changes,code", [
    ({"task_status": "running"}, "TASK_NOT_REVIEWABLE"),
    ({"task_suite_id": None}, "ACCEPTANCE_SUITE_REQUIRED"),
    ({"active_suite_id": None}, "ACCEPTANCE_SUITE_REQUIRED"),
    ({"active_suite_id": "new-suite"}, "ACCEPTANCE_POLICY_CHANGED"),
    ({"suite_digest": "bad"}, "INVALID_POLICY_DIGEST"),
    ({"candidate_digest": "bad"}, "INVALID_CANDIDATE_DIGEST"),
    ({"verification": {"exit_code": 0, "stdout": "100 passed"}}, "TRUSTED_VERIFICATION_REQUIRED"),
])
def test_acceptance_gate_rejects_missing_old_or_unbound_result(policy, baseline, changes, code):
    result = v.acceptance_gate(**{**gate_args(policy, baseline), **changes})
    assert result == {"allowed": False, "code": code}


@pytest.mark.parametrize("field,value", [
    ("suite_id", "another"), ("policy_digest", "0" * 64), ("candidate_digest", "0" * 64),
    ("passed", False), ("passed", 1), ("violations", [{"path": "a"}]), ("tests", {}),
])
def test_acceptance_gate_rechecks_report_instead_of_passed_flag(policy, baseline, field, value):
    args = gate_args(policy, baseline)
    args["verification"]["acceptance"][field] = value
    assert not v.acceptance_gate(**args)["allowed"]


def test_public_suite_omits_hidden_payload(policy):
    output = v.suite_public(suite(policy))
    assert set(output) == {"id", "project_id", "version", "name", "digest", "created_at"}
    assert "test_private" not in str(output)


async def test_create_suite_hashes_server_baseline_and_does_not_commit(monkeypatch, baseline):
    transport = AsyncMock(return_value=archive(baseline))
    monkeypatch.setattr(v, "broker", transport)
    session = SimpleNamespace(scalar=AsyncMock(return_value=4), add=Mock(), flush=AsyncMock(), commit=AsyncMock())
    project = SimpleNamespace(id="project", owner="owner", workspace_id="baseline")
    row = await v.create_suite(session, project, specification())
    assert row.version == 5 and row.owner == "owner"
    assert row.digest == row.policy["digest"]
    transport.assert_awaited_once_with("GET", "/workspaces/baseline/archive", raw=True)
    session.commit.assert_not_awaited()


async def test_verify_private_snapshot_and_omit_hidden_stdout(monkeypatch, policy, baseline):
    calls = []

    async def transport(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path.endswith("/archive"):
            return archive(baseline)
        if path.endswith("/verify"):
            assert set(kwargs["json"]) == {"files", "test_paths", "python_paths", "minimum_tests", "timeout_seconds"}
            return {"exit_code": 0, "passed": True, "tests": {**report(), "extra": "HIDDEN_TEST_SOURCE"}, "stdout": "HIDDEN_TEST_SOURCE", "stderr": "SECRET_CAPTURE"}
        return {}

    monkeypatch.setattr(v, "broker", transport)
    result = await v.verify_suite("original", suite(policy))
    snapshot = calls[0][2]["json"]["workspace_id"]
    assert snapshot != "original"
    assert calls[1][1] == f"/workspaces/{snapshot}/archive"
    assert calls[2][1] == f"/workspaces/{snapshot}/verify"
    assert calls[-1][:2] == ("DELETE", f"/workspaces/{snapshot}")
    assert result["acceptance"]["candidate_digest"] == v.content_digest(baseline)
    assert result["acceptance"]["passed"] and result["exit_code"] == 0
    assert "HIDDEN_TEST_SOURCE" not in str(result) and "SECRET_CAPTURE" not in str(result)


async def test_protected_mutation_blocks_runner(monkeypatch, policy, baseline):
    changed = {**baseline, "tests/test_public.py": b"# skipped"}
    transport = AsyncMock(side_effect=[{}, archive(changed), {}])
    monkeypatch.setattr(v, "broker", transport)
    result = await v.verify_suite("original", suite(policy))
    assert result["exit_code"] != 0 and not result["acceptance"]["passed"]
    assert not any(call.args[1].endswith("/verify") for call in transport.call_args_list)
    assert transport.call_args_list[-1].args[0] == "DELETE"


@pytest.mark.parametrize("result", [
    {"exit_code": 0, "passed": True, "tests": report(collected=0, passed=0)},
    {"exit_code": 0, "passed": True, "tests": report(skipped=1)},
    {"exit_code": 0, "passed": True, "tests": {}},
    {"exit_code": 1, "passed": True, "tests": report()},
    {"exit_code": 0, "passed": True, "tests": report(), "timed_out": True},
    {"exit_code": 0, "tests": report()},
])
async def test_verify_does_not_trust_broker_passed_flag(monkeypatch, policy, baseline, result):
    monkeypatch.setattr(v, "broker", AsyncMock(side_effect=[{}, archive(baseline), result, {}]))
    verified = await v.verify_suite("original", suite(policy))
    assert verified["exit_code"] != 0 and not verified["acceptance"]["passed"]


async def test_verify_cleanup_on_broker_failure(monkeypatch, policy, baseline):
    transport = AsyncMock(side_effect=[{}, archive(baseline), HTTPException(503, "offline"), {}])
    monkeypatch.setattr(v, "broker", transport)
    with pytest.raises(HTTPException):
        await v.verify_suite("original", suite(policy))
    assert transport.call_args_list[-1].args[0] == "DELETE"


async def test_tampered_policy_stops_before_any_broker_call(monkeypatch, policy):
    transport = AsyncMock()
    monkeypatch.setattr(v, "broker", transport)
    policy["files"]["test_private.py"] = "pass"
    with pytest.raises(HTTPException) as failure:
        await v.verify_suite("original", suite(policy))
    assert failure.value.status_code == 409
    transport.assert_not_awaited()

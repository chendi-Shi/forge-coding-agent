"""Fixed acceptance policy, isolated pytest execution, and Docker mount lifecycle."""

import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from agent_backend.sandbox import acceptance_runner
from agent_backend.sandbox.acceptance_runner import validate_verify
from agent_backend.sandbox.engine import SandboxError, SandboxManager, acceptance_tar
from agent_backend.sandbox.server import create_app

IDENTITY = "d309f8f5-04f1-47fd-bb59-37ff451f3b89"
TOKEN = "acceptance-private-test-token-with-enough-characters"
OPTIONS = {"test_paths": ["test_hidden.py"], "python_paths": ["."], "minimum_tests": 1, "timeout_seconds": 10}
SUITE = {"files": {"test_hidden.py": "def test_answer():\n    assert True\n"}, **OPTIONS}


@pytest.mark.parametrize("changes", [
    {"files": {"../test_bad.py": ""}}, {"files": {"/test_bad.py": ""}},
    {"files": {"conftest.py": ""}}, {"files": {"pytest.py": ""}},
    {"files": {"test_hidden.py": "x" * (256 * 1024 + 1)}},
    {"python_paths": ["../outside"]}, {"python_paths": ["/tmp"]},
    {"test_paths": ["missing_test.py"]}, {"test_paths": ["-k"]},
    {"minimum_tests": 0}, {"minimum_tests": True}, {"timeout_seconds": 121},
    {"HostConfig": {"Privileged": True}},
])
def test_acceptance_policy_rejects_unsafe_paths_and_overrides(changes):
    with pytest.raises(ValueError):
        validate_verify(SUITE | changes)


def test_suite_tar_contains_only_root_owned_readonly_files():
    files = {"nested/test_hidden.py": "assert True", "nested/helpers.py": "VALUE = 1"}
    with tarfile.open(fileobj=io.BytesIO(acceptance_tar(files))) as archive:
        assert set(archive.getnames()) == {"nested", *files}
        for member in archive:
            assert member.uid == 0 and member.gid == 0
            assert member.mode == (0o755 if member.isdir() else 0o444)


def run_pytest_case(tmp_path, candidate, suite):
    workspace, acceptance = tmp_path / "workspace", tmp_path / "acceptance"
    workspace.mkdir()
    acceptance.mkdir()
    for name, value in candidate.items():
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
    (acceptance / "test_hidden.py").write_text(suite, encoding="utf-8")
    config, report = tmp_path / "fixed.ini", tmp_path / "result.json"
    config.write_text("[pytest]\naddopts =\n", encoding="utf-8")
    program = """
import importlib.util, json, pathlib, sys
spec = importlib.util.spec_from_file_location('trusted_runner', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
result = module._run_pytest(json.loads(sys.argv[2]), workspace=pathlib.Path(sys.argv[3]), acceptance=pathlib.Path(sys.argv[4]), config=pathlib.Path(sys.argv[5]))
pathlib.Path(sys.argv[6]).write_text(json.dumps(result), encoding='utf-8')
"""
    # Host/repository configuration and plugin environment may not select tests.
    environment = {**os.environ, "PYTEST_ADDOPTS": "-k nonexistent", "PYTEST_PLUGINS": "nonexistent_plugin"}
    process = subprocess.run([
        sys.executable, "-I", "-c", program, str(Path(acceptance_runner.__file__).resolve()),
        json.dumps(OPTIONS), str(workspace), str(acceptance), str(config), str(report),
    ], env=environment, capture_output=True, text=True, timeout=20)
    assert process.returncode == 0, process.stderr
    return json.loads(report.read_text(encoding="utf-8"))


@pytest.mark.parametrize("candidate,suite,passed,counter", [
    ({"subject.py": "answer = 2"}, "from subject import answer\ndef test_answer():\n    assert answer == 2", True, "passed"),
    ({"subject.py": "answer = 1", "pytest.ini": "[pytest]\naddopts=-k nonexistent\n",
      "pytest.py": "raise RuntimeError('fake pytest')", "sitecustomize.py": "raise RuntimeError('fake site')",
      "conftest.py": "raise RuntimeError('repository conftest must not load')"},
     "from subject import answer\ndef test_answer():\n    assert answer == 2", False, "failed"),
    ({"subject.py": "print('100 passed; {\"passed\":true}')\nanswer = 1"},
     "from subject import answer\ndef test_answer():\n    assert answer == 2", False, "failed"),
    ({}, "VALUE = 1", False, "collected"),
    ({}, "import pytest\n@pytest.mark.skip\ndef test_skipped(): pass", False, "skipped"),
    ({}, "import pytest\n@pytest.mark.xfail\ndef test_xfail(): assert False", False, "xfailed"),
    ({}, "import pytest\n@pytest.mark.xfail\ndef test_xpass(): pass", False, "xpassed"),
    ({}, "def test_fail(): assert False", False, "failed"),
    ({}, "raise RuntimeError('bad collection')", False, "errors"),
])
def test_fixed_pytest_uses_real_statistics_and_fails_closed(tmp_path, candidate, suite, passed, counter):
    result = run_pytest_case(tmp_path, candidate, suite)
    assert result["passed"] is passed
    assert result["tests"][counter] == (0 if counter == "collected" else 1)


@pytest.mark.parametrize("filename", ["json.py", "packaging/__init__.py", "pytest/__init__.py"])
def test_candidate_import_target_cannot_resolve_to_an_image_package(tmp_path, filename):
    result = run_pytest_case(tmp_path, {filename: "CANDIDATE_ONLY = True"}, "def test_placeholder(): assert True")
    assert result["passed"] is False
    assert result["tests"]["collected"] == 0
    assert result["unsupported_reason"].startswith("import_collision:")


class VerifyEngine:
    def __init__(self, *, fail_upload=False):
        self.fail_upload = fail_upload
        self.policies, self.deleted, self.uploads, self.started, self.volumes = [], [], [], [], []

    def __call__(self, request):
        path, method = request.url.path, request.method
        if path.startswith("/images/"):
            return httpx.Response(200, json={"Id": "sha256:" + "b" * 64})
        if path == "/volumes/create":
            name = json.loads(request.content)["Name"]
            self.volumes.append(name)
            return httpx.Response(201, json={"Name": name})
        if method == "DELETE":
            self.deleted.append(path)
            return httpx.Response(204)
        if path == "/containers/create":
            self.policies.append(json.loads(request.content))
            return httpx.Response(201, json={"Id": str(len(self.policies)) * 64})
        if path.endswith("/archive"):
            self.uploads.append((request.url.params["path"], request.content))
            return httpx.Response(500 if self.fail_upload else 200)
        if path.endswith("/start"):
            self.started.append(path)
            return httpx.Response(204)
        if path.endswith("/wait"):
            return httpx.Response(200, json={"StatusCode": 0})
        if path.endswith("/json"):
            return httpx.Response(200, json={"State": {}})
        if path.endswith("/logs"):
            payload = json.dumps({"exit_code": 0, "passed": True, "tests": {"collected": 1, "passed": 1}}).encode()
            return httpx.Response(200, content=b"\x01\0\0\0" + len(payload).to_bytes(4, "big") + payload)
        raise AssertionError((method, path))


async def test_acceptance_is_uploaded_separately_and_only_execution_mount_is_readonly():
    stub = VerifyEngine()
    async with httpx.AsyncClient(transport=httpx.MockTransport(stub), base_url="http://docker") as client:
        result = await SandboxManager(client, image="sandbox").execute(IDENTITY, {"name": "verify", "arguments": OPTIONS}, acceptance=SUITE["files"])
    assert result["passed"]
    assert len(stub.policies) == 2
    for index, policy in enumerate(stub.policies):
        mount = next(mount for mount in policy["HostConfig"]["Mounts"] if mount["Target"] == "/acceptance")
        assert mount["ReadOnly"] is bool(index)
        assert mount["Source"] != policy["HostConfig"]["Mounts"][0]["Source"]
    assert stub.started == [f"/containers/{'2' * 64}/start"]
    assert [path for path, _ in stub.uploads] == ["/acceptance", "/input"]
    with tarfile.open(fileobj=io.BytesIO(stub.uploads[1][1])) as archive:
        request = json.load(archive.extractfile("request.json"))
        assert "files" not in request["arguments"]
    assert all(f"/volumes/{name}" in stub.deleted for name in stub.volumes)
    assert all(f"/containers/{number * 64}" in stub.deleted for number in ("1", "2"))


async def test_acceptance_upload_failure_cleans_staging_and_both_volumes():
    stub = VerifyEngine(fail_upload=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(stub), base_url="http://docker") as client:
        with pytest.raises(SandboxError):
            await SandboxManager(client, image="sandbox").execute(IDENTITY, {"name": "verify", "arguments": OPTIONS}, acceptance=SUITE["files"])
    assert not stub.started
    assert f"/containers/{'1' * 64}" in stub.deleted
    assert all(f"/volumes/{name}" in stub.deleted for name in stub.volumes)


async def test_verify_forks_candidate_and_deletes_only_its_private_copy(monkeypatch):
    async with httpx.AsyncClient() as client:
        manager = SandboxManager(client, image="sandbox")
        fork, execute, delete = AsyncMock(), AsyncMock(return_value={"passed": False}), AsyncMock()
        monkeypatch.setattr(manager, "fork", fork)
        monkeypatch.setattr(manager, "execute", execute)
        monkeypatch.setattr(manager, "delete", delete)
        assert await manager.verify(IDENTITY, SUITE) == {"passed": False}
    temporary = fork.await_args.args[1]
    assert temporary != IDENTITY
    delete.assert_awaited_once_with(temporary)
    assert execute.await_args.args[0] == temporary


async def test_verify_private_endpoint_exists_but_is_not_a_model_tool():
    manager = AsyncMock()
    manager.verify.return_value = {"passed": False, "exit_code": 1}
    app = create_app(manager, TOKEN)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://broker") as client:
        assert (await client.post(f"/workspaces/{IDENTITY}/verify", json=SUITE)).status_code == 401
        client.headers["Authorization"] = "Bearer " + TOKEN
        response = await client.post(f"/workspaces/{IDENTITY}/verify", json=SUITE)
        assert response.status_code == 200 and response.json()["passed"] is False
        assert (await client.post(f"/workspaces/{IDENTITY}/tools", json={"name": "verify", "arguments": SUITE})).status_code == 400
    manager.verify.assert_awaited_once_with(IDENTITY, SUITE)

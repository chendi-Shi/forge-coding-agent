import io
import json
import tarfile
import time
from uuid import uuid4

import httpx
import pytest

from agent_backend.sandbox.engine import (
    DEADLINE_LABEL,
    OWNER_LABEL,
    ROLE_LABEL,
    SOURCE_LABEL,
    WORKSPACE_LABEL,
    SandboxError,
    SandboxManager,
    container_policy,
    demultiplex,
    input_tar,
    validate_command,
    volume_name,
    workspace_id,
)
from agent_backend.sandbox.runner import checked_cwd

IDENTITY = "d309f8f5-04f1-47fd-bb59-37ff451f3b89"
CONTAINER = "a" * 64


def frame(value):
    data = json.dumps(value).encode()
    return b"\x01\0\0\0" + len(data).to_bytes(4, "big") + data


def test_policy_never_exposes_host_or_secrets_and_protects_baseline():
    policy = container_policy("sha256:fixed", "work", "input", IDENTITY, 1, command=True)
    host = policy["HostConfig"]
    assert policy["User"] == "10001:10001"
    assert policy["WorkingDir"] == "/workspace"
    assert host["NetworkMode"] == "none" and host["ReadonlyRootfs"]
    assert not host["Privileged"] and host["CapDrop"] == ["ALL"]
    assert host["Memory"] == host["MemorySwap"] == 1024**3
    assert host["PidsLimit"] == 128 and host["NanoCpus"] == 10**9
    assert all(mount["Type"] == "volume" for mount in host["Mounts"])
    git_mount = next(mount for mount in host["Mounts"] if mount["Target"] == "/workspace/.git")
    assert git_mount["ReadOnly"] and git_mount["VolumeOptions"]["Subpath"] == ".git"
    assert "docker.sock" not in json.dumps(policy)
    assert all(value.split("=", 1)[0] in {"HOME", "LANG", "PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED"} for value in policy["Env"])


@pytest.mark.parametrize("value", ["../other", "ABC", IDENTITY.upper(), IDENTITY.replace("-", ""), None, []])
def test_noncanonical_workspace_ids_are_rejected(value):
    with pytest.raises(SandboxError, match="INVALID_WORKSPACE_ID"):
        workspace_id(value)


@pytest.mark.parametrize("arguments", [
    {}, {"argv": []}, {"command": "x", "argv": ["x"]}, {"command": "x", "timeout_seconds": 301},
    {"command": "x", "timeout_seconds": True}, {"command": "x", "timeout_seconds": float("nan")},
    {"argv": ["x\0y"]}, {"command": "x", "max_output_bytes": 10**8}, {"command": "x", "env": {"API_KEY": "secret"}},
])
def test_command_resource_and_environment_overrides_are_rejected(arguments):
    with pytest.raises(SandboxError):
        validate_command(arguments)


def test_request_archive_has_fixed_nonroot_ownership_and_no_dynamic_paths():
    payload = {"name": "read_file", "arguments": {"path": "a.py"}}
    with tarfile.open(fileobj=io.BytesIO(input_tar(payload))) as archive:
        assert archive.getnames() == ["request.json"]
        entry = archive.getmember("request.json")
        assert (entry.uid, entry.gid, entry.mode) == (10001, 10001, 0o600)
        assert json.load(archive.extractfile(entry)) == payload


@pytest.mark.parametrize("data", [b"{}", b"\x01\0\0\0\0\0\0\x10hi", b"\x03\0\0\0\0\0\0\0"])
def test_invalid_docker_output_is_rejected(data):
    with pytest.raises(SandboxError):
        demultiplex(data)


@pytest.mark.parametrize("path", ["../escape", "/tmp", ".git", "a/../../b", "a\\b"])
def test_command_cwd_cannot_escape_workspace(tmp_path, path):
    with pytest.raises(ValueError):
        checked_cwd(path, tmp_path)


class EngineStub:
    def __init__(self, *, fail_start=False, oom=False, busy=False):
        self.requests = []
        self.policy = None
        self.upload = None
        self.volumes = {}
        self.fail_start = fail_start
        self.oom = oom
        self.busy = busy

    def __call__(self, request):
        path, method = request.url.path, request.method
        self.requests.append((method, path))
        if method == "GET" and path.startswith("/images/"):
            return httpx.Response(200, json={"Id": "sha256:" + "b" * 64})
        if method == "POST" and path == "/volumes/create":
            body = json.loads(request.content)
            self.volumes[body["Name"]] = body
            return httpx.Response(201, json=body)
        if path.startswith("/volumes/"):
            name = path.removeprefix("/volumes/")
            if method == "GET":
                return httpx.Response(200, json=self.volumes[name]) if name in self.volumes else httpx.Response(404)
            if method == "DELETE":
                self.volumes.pop(name, None)
                return httpx.Response(204)
        if method == "POST" and path == "/containers/create":
            assert request.url.params["name"] == "agent-backend-exec-" + IDENTITY
            self.policy = json.loads(request.content)
            return httpx.Response(409) if self.busy else httpx.Response(201, json={"Id": CONTAINER})
        if method == "PUT" and path.endswith("/archive"):
            self.upload = request.content
            assert request.url.params["path"] == "/input"
            return httpx.Response(200)
        if path.endswith("/start"):
            return httpx.Response(500 if self.fail_start else 204)
        if path.endswith("/wait"):
            return httpx.Response(200, json={"StatusCode": 0})
        if path.endswith("/json"):
            return httpx.Response(200, json={"State": {"OOMKilled": self.oom}})
        if path.endswith("/logs"):
            return httpx.Response(200, content=frame({"exit_code": 0, "stdout": "passed", "stderr": "", "truncated": False, "duration": 0.1}))
        if method == "DELETE" and path == f"/containers/{CONTAINER}":
            assert request.url.params["force"] == "true"
            return httpx.Response(204)
        raise AssertionError((method, path))


async def test_complete_execution_uses_restricted_container_and_removes_only_ephemeral_data():
    stub = EngineStub()
    async with httpx.AsyncClient(transport=httpx.MockTransport(stub), base_url="http://docker") as client:
        manager = SandboxManager(client, image="agent-backend-sandbox:0.1.0")
        result = await manager.execute(IDENTITY, {"name": "run_command", "arguments": {"argv": ["pytest", "-q"]}})
    assert result["exit_code"] == 0 and result["stdout"] == "passed"
    assert stub.policy["Image"] == "sha256:" + "b" * 64
    with tarfile.open(fileobj=io.BytesIO(stub.upload)) as archive:
        assert json.load(archive.extractfile("request.json"))["arguments"] == {"argv": ["pytest", "-q"]}
    assert ("DELETE", f"/containers/{CONTAINER}") in stub.requests
    assert not stub.volumes
    assert ("DELETE", "/volumes/" + volume_name(IDENTITY)) not in stub.requests


async def test_failed_start_always_cleans_container_and_input_volume():
    stub = EngineStub(fail_start=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(stub), base_url="http://docker") as client:
        with pytest.raises(SandboxError, match="SANDBOX_ENGINE_OPERATION_FAILED"):
            await SandboxManager(client, image="sandbox").execute(IDENTITY, {"name": "get_diff", "arguments": {}})
    assert ("DELETE", f"/containers/{CONTAINER}") in stub.requests
    assert not stub.volumes


async def test_prior_execution_blocks_overlap_without_deleting_its_container():
    stub = EngineStub(busy=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(stub), base_url="http://docker") as client:
        with pytest.raises(SandboxError, match="WORKSPACE_BUSY") as error:
            await SandboxManager(client, image="sandbox").execute(IDENTITY, {"name": "get_diff", "arguments": {}})
    assert error.value.status == 409
    assert not stub.volumes
    assert not any(method == "DELETE" and path.startswith("/containers/") for method, path in stub.requests)


async def test_memory_failure_is_reported_without_collecting_unbounded_logs():
    stub = EngineStub(oom=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(stub), base_url="http://docker") as client:
        result = await SandboxManager(client, image="sandbox").execute(IDENTITY, {"name": "get_diff", "arguments": {}})
    assert result["error"]["code"] == "SANDBOX_MEMORY_LIMIT"
    assert not any(path.endswith("/logs") for _, path in stub.requests)
    assert not stub.volumes


async def test_volume_ownership_is_checked_before_running_or_deleting():
    stub = EngineStub()
    stub.volumes[volume_name(IDENTITY)] = {"Labels": {OWNER_LABEL: "true", WORKSPACE_LABEL: str(uuid4()), ROLE_LABEL: "workspace"}}
    async with httpx.AsyncClient(transport=httpx.MockTransport(stub), base_url="http://docker") as client:
        manager = SandboxManager(client, image="sandbox")
        for operation in (manager.delete(IDENTITY), manager.tool(IDENTITY, "get_diff", {})):
            with pytest.raises(SandboxError, match="WORKSPACE_OWNERSHIP_MISMATCH"):
                await operation
    assert stub.requests == [("GET", "/volumes/" + volume_name(IDENTITY))] * 2


async def test_fork_snapshots_current_tree_into_new_baseline(monkeypatch):
    calls = []
    async with httpx.AsyncClient() as client:
        manager = SandboxManager(client, image="sandbox")

        async def tool(identity, name, arguments):
            calls.append((identity, name, arguments))
            return {"archive_base64": "emlw"}

        async def create(identity, arguments, *, source):
            assert source == IDENTITY
            calls.append((identity, "initialize", arguments))
            return {"workspace_id": identity, "baseline": "new"}

        monkeypatch.setattr(manager, "tool", tool)
        monkeypatch.setattr(manager, "create", create)
        target = str(uuid4())
        assert (await manager.fork(IDENTITY, target))["baseline"] == "new"
    assert calls == [(IDENTITY, "archive", {}), (target, "initialize", {"archive_base64": "emlw"})]


async def test_fork_retry_reuses_only_an_initialized_destination_from_the_same_source(monkeypatch):
    stub = EngineStub()
    source = str(uuid4())
    stub.volumes[volume_name(IDENTITY)] = {"Labels": {
        OWNER_LABEL: "true", WORKSPACE_LABEL: IDENTITY, ROLE_LABEL: "workspace", SOURCE_LABEL: source,
    }}
    async with httpx.AsyncClient(transport=httpx.MockTransport(stub), base_url="http://docker") as client:
        manager = SandboxManager(client, image="sandbox")
        verified = []

        async def execute(identity, payload):
            verified.append(payload["name"])
            return {"diff": "", "truncated": False}

        monkeypatch.setattr(manager, "execute", execute)
        assert await manager.create(IDENTITY, {"archive_base64": "ignored retry"}, source=source) == {"workspace_id": IDENTITY, "existing": True}
        assert verified == ["get_diff"]
        with pytest.raises(SandboxError, match="WORKSPACE_ALREADY_EXISTS"):
            await manager.create(IDENTITY, {"archive_base64": "new input"}, source=str(uuid4()))
        with pytest.raises(SandboxError, match="WORKSPACE_ALREADY_EXISTS"):
            await manager.create(IDENTITY, {"files": []})


async def test_janitor_only_removes_expired_owned_execution_resources():
    expired = {OWNER_LABEL: "true", ROLE_LABEL: "execution", DEADLINE_LABEL: str(time.time() - 10)}
    future = {**expired, DEADLINE_LABEL: str(time.time() + 999)}
    input_name = "agent-backend-input-" + "c" * 32
    deleted = []

    def handler(request):
        if request.method == "DELETE":
            deleted.append(request.url.path)
            return httpx.Response(204)
        if request.url.path == "/containers/json":
            return httpx.Response(200, json=[
                {"Id": CONTAINER, "Labels": expired}, {"Id": "b" * 64, "Labels": future},
                {"Id": "c" * 64, "Labels": {**expired, OWNER_LABEL: "false"}},
            ])
        if request.url.path == "/volumes":
            return httpx.Response(200, json={"Volumes": [
                {"Name": input_name, "Labels": {**expired, ROLE_LABEL: "input"}},
                {"Name": volume_name(IDENTITY), "Labels": {**expired, ROLE_LABEL: "workspace"}},
            ]})
        raise AssertionError(request.url)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://docker") as client:
        await SandboxManager(client, image="sandbox").reap()
    assert deleted == [f"/containers/{CONTAINER}", "/volumes/" + input_name]

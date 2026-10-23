"""Small fixed-policy Docker Engine client. User input never becomes HostConfig."""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import tarfile
import time
from contextlib import suppress
from typing import Any
from urllib.parse import quote
from uuid import UUID, uuid4
from weakref import WeakValueDictionary

import httpx

OWNER_LABEL = "agent-backend.sandbox"
WORKSPACE_LABEL = "agent-backend.workspace-id"
SOURCE_LABEL = "agent-backend.source-workspace-id"
ROLE_LABEL = "agent-backend.sandbox-role"
DEADLINE_LABEL = "agent-backend.sandbox-deadline"
MAX_PAYLOAD = 32 * 1024 * 1024
TOOLS = {"list_files", "read_file", "write_file", "replace_text", "delete_file", "run_command", "get_diff"}


class SandboxError(Exception):
    def __init__(self, code: str, status: int = 400):
        self.code, self.status = code, status
        super().__init__(code)


def workspace_id(value: str) -> str:
    try:
        parsed = str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise SandboxError("INVALID_WORKSPACE_ID") from None
    if parsed != value:
        raise SandboxError("INVALID_WORKSPACE_ID")
    return parsed


def volume_name(value: str) -> str:
    return "agent-backend-workspace-" + workspace_id(value)


def validate_command(arguments: dict[str, Any]) -> float:
    command, argv = arguments.get("command"), arguments.get("argv")
    if (command is None) == (argv is None):
        raise SandboxError("PROVIDE_COMMAND_OR_ARGV")
    if command is not None and (not isinstance(command, str) or not command.strip() or len(command) > 32768 or "\x00" in command):
        raise SandboxError("INVALID_COMMAND")
    if argv is not None and (not isinstance(argv, list) or not argv or len(argv) > 256 or not all(isinstance(item, str) and len(item) <= 32768 and "\x00" not in item for item in argv)):
        raise SandboxError("INVALID_ARGV")
    duration = arguments.get("timeout_seconds", 60)
    cap = arguments.get("max_output_bytes", 65536)
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not 0 < duration <= 300:
        raise SandboxError("INVALID_COMMAND_TIMEOUT")
    if isinstance(cap, bool) or not isinstance(cap, int) or not 1024 <= cap <= 1048576:
        raise SandboxError("INVALID_OUTPUT_LIMIT")
    if set(arguments) - {"command", "argv", "cwd", "timeout_seconds", "max_output_bytes"}:
        raise SandboxError("UNSUPPORTED_COMMAND_OPTION")
    return float(duration)


def input_tar(payload: dict[str, Any]) -> bytes:
    content = json.dumps(payload, ensure_ascii=True).encode()
    if len(content) > MAX_PAYLOAD:
        raise SandboxError("REQUEST_TOO_LARGE", 413)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        info = tarfile.TarInfo("request.json")
        info.size, info.uid, info.gid, info.mode = len(content), 10001, 10001, 0o600
        archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def demultiplex(data: bytes) -> bytes:
    """Docker non-TTY stdout/stderr uses an eight-byte frame header."""
    output = bytearray()
    offset = 0
    while offset < len(data):
        if len(data) - offset < 8 or data[offset] not in {1, 2} or data[offset + 1:offset + 4] != b"\0\0\0":
            raise SandboxError("INVALID_CONTAINER_OUTPUT", 502)
        length = int.from_bytes(data[offset + 4:offset + 8], "big")
        offset += 8
        if length > len(data) - offset:
            raise SandboxError("INVALID_CONTAINER_OUTPUT", 502)
        output.extend(data[offset:offset + length])
        offset += length
    return bytes(output)


def container_policy(image: str, work_volume: str, input_volume: str, identity: str, deadline: float, *, command: bool = False) -> dict[str, Any]:
    policy = {
        "Image": image, "User": "10001:10001", "WorkingDir": "/workspace",
        "Entrypoint": ["python", "/opt/sandbox/entrypoint.py"], "Cmd": [],
        "Env": ["HOME=/tmp/home", "LANG=C.UTF-8", "PYTHONDONTWRITEBYTECODE=1", "PYTHONUNBUFFERED=1"],
        "AttachStdout": True, "AttachStderr": True, "Tty": False,
        "Labels": {OWNER_LABEL: "true", WORKSPACE_LABEL: identity, ROLE_LABEL: "execution", DEADLINE_LABEL: str(deadline)},
        "HostConfig": {
            "ReadonlyRootfs": True, "Privileged": False, "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"], "NetworkMode": "none", "Init": True,
            "PidsLimit": 128, "Memory": 1024 * 1024 * 1024, "MemorySwap": 1024 * 1024 * 1024,
            "NanoCpus": 1000000000, "ShmSize": 64 * 1024 * 1024,
            "Ulimits": [{"Name": "nofile", "Soft": 1024, "Hard": 1024}, {"Name": "core", "Soft": 0, "Hard": 0}],
            "Mounts": [
                {"Type": "volume", "Source": work_volume, "Target": "/workspace", "ReadOnly": False},
                {"Type": "volume", "Source": input_volume, "Target": "/input", "ReadOnly": False},
            ],
            "Tmpfs": {"/tmp": "rw,noexec,nosuid,nodev,size=134217728,uid=10001,gid=10001"},
            "LogConfig": {"Type": "json-file", "Config": {"max-size": "32m", "max-file": "1"}},
            "RestartPolicy": {"Name": "no"},
        },
    }
    if command:
        # Untrusted tests may not rewrite the accepted baseline. Volume subpaths
        # require Docker Engine 26+; file helpers retain ordinary Git access.
        policy["HostConfig"]["Mounts"].append({
            "Type": "volume", "Source": work_volume, "Target": "/workspace/.git", "ReadOnly": True,
            "VolumeOptions": {"Subpath": ".git", "NoCopy": True},
        })
    return policy


class SandboxManager:
    def __init__(self, client: httpx.AsyncClient, *, image: str, concurrency: int = 2):
        self.client, self.image = client, image
        self.slots = asyncio.Semaphore(concurrency)
        self.locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    def lock(self, identity: str) -> asyncio.Lock:
        return self.locks.setdefault(workspace_id(identity), asyncio.Lock())

    async def request(self, method: str, path: str, *, allowed: set[int] | None = None, **kwargs: Any) -> httpx.Response:
        try:
            response = await self.client.request(method, path, **kwargs)
        except httpx.HTTPError:
            raise SandboxError("SANDBOX_ENGINE_UNAVAILABLE", 503) from None
        if response.is_error and response.status_code not in (allowed or set()):
            raise SandboxError("SANDBOX_ENGINE_OPERATION_FAILED", 503)
        return response

    async def check_volume(self, identity: str, *, missing_ok: bool = False) -> bool:
        response = await self.request("GET", "/volumes/" + volume_name(identity), allowed={404})
        if response.status_code == 404:
            if missing_ok:
                return False
            raise SandboxError("WORKSPACE_NOT_FOUND", 404)
        labels = response.json().get("Labels") or {}
        if labels.get(OWNER_LABEL) != "true" or labels.get(WORKSPACE_LABEL) != identity or labels.get(ROLE_LABEL) != "workspace":
            raise SandboxError("WORKSPACE_OWNERSHIP_MISMATCH", 403)
        return True

    async def create(self, identity: str, arguments: dict[str, Any], *, source: str | None = None) -> dict[str, Any]:
        identity = workspace_id(identity)
        async with self.lock(identity):
            if await self.check_volume(identity, missing_ok=True):
                if source is not None:
                    labels = (await self.request("GET", "/volumes/" + volume_name(identity))).json().get("Labels") or {}
                    if labels.get(SOURCE_LABEL) == source:
                        # A retry must retain the original forked snapshot. A
                        # successful Git operation distinguishes initialization
                        # completed before a lost reply from an incomplete volume.
                        result = await self.execute(identity, {"name": "get_diff", "arguments": {"max_output": 1}})
                        if "error" in result:
                            raise SandboxError("WORKSPACE_INITIALIZATION_INCOMPLETE", 422)
                        return {"workspace_id": identity, "existing": True}
                raise SandboxError("WORKSPACE_ALREADY_EXISTS", 409)
            labels = {OWNER_LABEL: "true", WORKSPACE_LABEL: identity, ROLE_LABEL: "workspace"}
            if source is not None:
                labels[SOURCE_LABEL] = workspace_id(source)
            await self.request("POST", "/volumes/create", json={"Name": volume_name(identity), "Labels": labels})
            try:
                result = await self.execute(identity, {"name": "initialize", "arguments": arguments})
                if "error" in result:
                    raise SandboxError("WORKSPACE_INITIALIZATION_REJECTED", 422)
                return {"workspace_id": identity, **result}
            except BaseException:
                with suppress(SandboxError):
                    await self.request("DELETE", "/volumes/" + volume_name(identity), allowed={404})
                raise

    async def tool(self, identity: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        identity = workspace_id(identity)
        if not isinstance(name, str) or name not in TOOLS | {"archive", "restore"} or not isinstance(arguments, dict):
            raise SandboxError("INVALID_TOOL")
        if name == "run_command":
            validate_command(arguments)
        async with self.lock(identity):
            await self.check_volume(identity)
            return await self.execute(identity, {"name": name, "arguments": arguments})

    async def fork(self, source: str, target: str) -> dict[str, Any]:
        source, target = workspace_id(source), workspace_id(target)
        if source == target:
            raise SandboxError("FORK_REQUIRES_NEW_WORKSPACE")
        snapshot = await self.tool(source, "archive", {})
        if "error" in snapshot or not isinstance(snapshot.get("archive_base64"), str):
            raise SandboxError("WORKSPACE_SNAPSHOT_FAILED", 422)
        return await self.create(target, {"archive_base64": snapshot["archive_base64"]}, source=source)

    async def delete(self, identity: str) -> dict[str, Any]:
        identity = workspace_id(identity)
        async with self.lock(identity):
            if not await self.check_volume(identity, missing_ok=True):
                return {"workspace_id": identity, "deleted": False}
            await self.request("DELETE", "/volumes/" + volume_name(identity))
            return {"workspace_id": identity, "deleted": True}

    async def execute(self, identity: str, payload: dict[str, Any]) -> dict[str, Any]:
        duration = validate_command(payload["arguments"]) if payload["name"] == "run_command" else 60.0
        upload = input_tar(payload)
        async with self.slots:
            execution_id = uuid4().hex
            input_volume = "agent-backend-input-" + execution_id
            container_id: str | None = None
            input_created = False
            deadline = time.time() + duration + 90
            started = time.monotonic()
            try:
                image = (await self.request("GET", "/images/" + quote(self.image, safe="") + "/json")).json().get("Id")
                if not isinstance(image, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
                    raise SandboxError("SANDBOX_IMAGE_NOT_AVAILABLE", 503)
                await self.request("POST", "/volumes/create", json={"Name": input_volume, "Labels": {OWNER_LABEL: "true", ROLE_LABEL: "input", DEADLINE_LABEL: str(deadline)}})
                input_created = True
                policy = container_policy(image, volume_name(identity), input_volume, identity, deadline, command=payload["name"] == "run_command")
                policy["Labels"]["agent-backend.input-volume"] = input_volume
                # The Engine's unique name is a second lock across broker restarts.
                # An abandoned container blocks reuse until its deadline janitor
                # removes it, so two processes cannot mutate one workspace.
                created = await self.request("POST", "/containers/create", params={"name": "agent-backend-exec-" + identity}, json=policy, allowed={409})
                if created.status_code == 409:
                    raise SandboxError("WORKSPACE_BUSY", 409)
                container_id = created.json().get("Id")
                if not isinstance(container_id, str) or not re.fullmatch(r"[0-9a-f]{64}", container_id):
                    raise SandboxError("INVALID_CONTAINER_ID", 503)
                await self.request("PUT", f"/containers/{container_id}/archive", params={"path": "/input"}, headers={"Content-Type": "application/x-tar"}, content=upload, timeout=30)
                await self.request("POST", f"/containers/{container_id}/start")
                try:
                    async with asyncio.timeout(duration + 10):
                        await self.request("POST", f"/containers/{container_id}/wait", params={"condition": "not-running"}, timeout=duration + 15)
                except TimeoutError:
                    raise SandboxError("SANDBOX_EXECUTION_TIMEOUT", 504) from None
                inspection = (await self.request("GET", f"/containers/{container_id}/json")).json()
                if inspection.get("State", {}).get("OOMKilled"):
                    return {
                        "error": {"code": "SANDBOX_MEMORY_LIMIT", "message": "Command exceeded its memory limit"},
                        "exit_code": 137, "stdout": "", "stderr": "Command exceeded its memory limit",
                        "truncated": False, "duration": round(time.monotonic() - started, 6), "timed_out": False,
                    }
                logs = await self.request("GET", f"/containers/{container_id}/logs", params={"stdout": "true", "stderr": "true", "tail": "all"}, timeout=30)
                if len(logs.content) > MAX_PAYLOAD:
                    raise SandboxError("SANDBOX_OUTPUT_LIMIT", 502)
                try:
                    result = json.loads(demultiplex(logs.content))
                except (ValueError, UnicodeError):
                    raise SandboxError("INVALID_SANDBOX_RESULT", 502) from None
                if not isinstance(result, dict):
                    raise SandboxError("INVALID_SANDBOX_RESULT", 502)
                return result
            finally:
                if container_id is not None and re.fullmatch(r"[0-9a-f]{64}", container_id):
                    with suppress(SandboxError):
                        await self.request("DELETE", f"/containers/{container_id}", params={"force": "true"}, allowed={404})
                if input_created:
                    with suppress(SandboxError):
                        await self.request("DELETE", "/volumes/" + input_volume, allowed={404, 409})

    async def reap(self) -> None:
        """Remove only expired execution containers/input volumes; retain workspaces."""
        filters = json.dumps({"label": [OWNER_LABEL + "=true", ROLE_LABEL + "=execution"]})
        containers = (await self.request("GET", "/containers/json", params={"all": "true", "filters": filters})).json()
        for container in containers:
            labels = container.get("Labels") or {}
            try:
                expired = float(labels[DEADLINE_LABEL]) < time.time()
            except (KeyError, ValueError, TypeError):
                continue
            identity = container.get("Id", "")
            if expired and labels.get(OWNER_LABEL) == "true" and labels.get(ROLE_LABEL) == "execution" and re.fullmatch(r"[0-9a-f]{64}", identity):
                await self.request("DELETE", f"/containers/{identity}", params={"force": "true"}, allowed={404})
        filters = json.dumps({"label": [OWNER_LABEL + "=true", ROLE_LABEL + "=input"]})
        volumes = (await self.request("GET", "/volumes", params={"filters": filters})).json().get("Volumes") or []
        for volume in volumes:
            labels = volume.get("Labels") or {}
            try:
                expired = float(labels[DEADLINE_LABEL]) < time.time()
            except (KeyError, ValueError, TypeError):
                continue
            name = volume.get("Name", "")
            if expired and labels.get(OWNER_LABEL) == "true" and labels.get(ROLE_LABEL) == "input" and re.fullmatch(r"agent-backend-input-[0-9a-f]{32}", name):
                await self.request("DELETE", "/volumes/" + name, allowed={404, 409})


def engine_client() -> httpx.AsyncClient:
    socket_path = os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock")
    return httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=socket_path), base_url="http://docker", timeout=15, trust_env=False)

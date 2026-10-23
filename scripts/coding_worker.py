#!/usr/bin/env python3
"""Pull coding jobs and run the authenticated local Codex CLI in isolated repositories.

Requires Python 3.12+ and httpx. Credentials stay in the HTTP client; Codex reuses
its own login. This worker never treats the model's test claim as verification.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import ctypes
import io
import json
import os
import re
import shutil
import signal
import socket
import stat
import sys
import time
import zipfile
from collections import deque
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
VERSION = "1.0.0"
API_PREFIX = "/api/coding"
MAX_ARCHIVE_BYTES = 16 * 1024 * 1024  # Coding API compressed archive limit.
MAX_SOURCE_BYTES = 20 * 1024 * 1024  # sandbox/helper.py restore limit.
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_FILES = 2000
MAX_EVENT_BYTES = 1024 * 1024
MAX_STDERR_BYTES = 64 * 1024
HEARTBEAT_INTERVAL_SECONDS = 5
EXCLUDED_PARTS = frozenset({
    ".git", ".codex", ".ssh", ".aws", ".azure", "node_modules", ".venv", "venv",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".nox",
    ".cache", ".coding-workspaces", ".sandbox", ".sandbox-internal",
})
WINDOWS_DEVICES = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.I)


class WorkerError(Exception):
    """A credential-free, stable code suitable for local logs and API errors."""


def log(event: str, **fields: Any) -> None:
    print(json.dumps({"timestamp": datetime.now(UTC).isoformat(), "event": event, **fields}), flush=True)


def read_auth_token(env_path: Path = PROJECT_ROOT / ".env") -> str:
    token = os.environ.get("AUTH_TOKEN", "").strip()
    if token:
        return token
    try:
        lines = env_path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        raise WorkerError("MISSING_AUTH_TOKEN") from None
    for line in lines:
        name, separator, value = line.strip().removeprefix("export ").partition("=")
        if separator and name.strip() == "API_KEYS_JSON":
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            try:
                keys = json.loads(value)
                token = keys["local-client"]
                if not isinstance(token, str) or not token.strip():
                    raise ValueError
                return token.strip()
            except (ValueError, KeyError, TypeError):
                raise WorkerError("MISSING_LOCAL_CLIENT_TOKEN") from None
    raise WorkerError("MISSING_AUTH_TOKEN")


def child_environment() -> dict[str, str]:
    # Allow only OS/runtime discovery settings. In particular, do not pass the
    # backend bearer token, lease, DB passwords, or API keys to repository code.
    allowed = {
        "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP",
        "USERPROFILE", "HOME", "HOMEDRIVE", "HOMEPATH", "LOCALAPPDATA", "APPDATA",
        "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA", "OS", "USERNAME",
        "PROCESSOR_ARCHITECTURE", "LANG", "LC_ALL", "CODEX_HOME", "TERM",
    }
    result = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    result.update({"PYTHONIOENCODING": "utf-8", "NO_COLOR": "1", "GIT_TERMINAL_PROMPT": "0",
                   "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
    return result


def redact(value: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/-]+=*", r"\1[REDACTED]", value)
        return re.sub(r"\bsk-[A-Za-z0-9_-]{16,}", "[REDACTED]", value)
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    if isinstance(value, dict):
        return {redact(key, secrets): redact(item, secrets) for key, item in value.items()}
    return value


def excluded(parts: tuple[str, ...]) -> bool:
    return any(part.casefold() in EXCLUDED_PARTS or part.casefold() == ".env"
               or part.casefold().startswith(".env.") for part in parts)


def safe_zip_name(name: str) -> tuple[str, ...]:
    if "\\" in name or "\x00" in name or name.startswith("/"):
        raise WorkerError("UNSAFE_SOURCE_PATH")
    parts = PurePosixPath(name).parts
    if not parts or any(part in {".", ".."} or ":" in part or part.endswith((".", " "))
                        or WINDOWS_DEVICES.match(part) for part in parts):
        raise WorkerError("UNSAFE_SOURCE_PATH")
    return parts


def extract_source(data: bytes, repo: Path) -> None:
    if len(data) > MAX_ARCHIVE_BYTES:
        raise WorkerError("SOURCE_ARCHIVE_TOO_LARGE")
    repo.mkdir(parents=True, exist_ok=False)
    total = 0
    seen: set[str] = set()
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > 3000 or sum(not entry.is_dir() for entry in entries) > MAX_FILES:
                raise WorkerError("SOURCE_TOO_MANY_FILES")
            for info in entries:
                parts = safe_zip_name(info.filename)
                mode = info.external_attr >> 16
                if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}):
                    raise WorkerError("SOURCE_SPECIAL_FILE")
                if excluded(parts):
                    continue
                key = "/".join(parts).casefold()
                if key in seen:
                    raise WorkerError("SOURCE_DUPLICATE_PATH")
                seen.add(key)
                total += info.file_size
                if total > MAX_SOURCE_BYTES or info.file_size > MAX_FILE_BYTES or info.flag_bits & 1:
                    raise WorkerError("SOURCE_SIZE_OR_ENCRYPTION_REJECTED")
                target = repo.joinpath(*parts)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info) as source, target.open("xb") as destination:
                        shutil.copyfileobj(source, destination, length=64 * 1024)
                    if os.name != "nt" and mode & 0o111:
                        target.chmod(0o755)
    except (zipfile.BadZipFile, RuntimeError, OSError, ValueError):
        raise WorkerError("INVALID_SOURCE_ARCHIVE") from None


def is_link_or_junction(path: Path) -> bool:
    metadata = path.lstat()
    return stat.S_ISLNK(metadata.st_mode) or bool(getattr(metadata, "st_file_attributes", 0) & 0x400)


def pack_workspace(repo: Path) -> bytes:
    """Package ordinary source files only; refuse escaping links and ZIP bombs."""
    if not repo.is_dir():
        return empty_archive()
    if is_link_or_junction(repo):
        raise WorkerError("OUTPUT_WORKSPACE_IS_LINK")
    buffer = io.BytesIO()
    total = count = 0
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for directory, dirs, files in os.walk(repo, followlinks=False):
            base = Path(directory)
            dirs[:] = sorted(name for name in dirs if not excluded((name,))
                             and not is_link_or_junction(base / name))
            for name in sorted(files):
                path = base / name
                relative = path.relative_to(repo)
                if excluded(relative.parts) or is_link_or_junction(path):
                    continue
                metadata = path.stat()
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink > 1:
                    continue
                count += 1
                total += metadata.st_size
                if count > MAX_FILES or total > MAX_SOURCE_BYTES or metadata.st_size > MAX_FILE_BYTES:
                    raise WorkerError("OUTPUT_WORKSPACE_TOO_LARGE")
                archive.write(path, relative.as_posix())
                if buffer.tell() > MAX_ARCHIVE_BYTES:
                    raise WorkerError("OUTPUT_ARCHIVE_TOO_LARGE")
    result = buffer.getvalue()
    if len(result) > MAX_ARCHIVE_BYTES:
        raise WorkerError("OUTPUT_ARCHIVE_TOO_LARGE")
    return result


def empty_archive() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w"):
        pass
    return buffer.getvalue()


class WindowsJob:
    """Kill the attached process and descendants if the worker exits or closes it."""

    def __init__(self, pid: int):
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class IOCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IOCounters),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.api.CreateJobObjectW.restype = wintypes.HANDLE
        self.api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.api.SetInformationJobObject.restype = wintypes.BOOL
        self.api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.api.OpenProcess.restype = wintypes.HANDLE
        self.api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.api.AssignProcessToJobObject.restype = wintypes.BOOL
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.api.CloseHandle.restype = wintypes.BOOL
        self.handle = self.api.CreateJobObjectW(None, None)
        process = None
        try:
            if not self.handle:
                raise WorkerError("WINDOWS_JOB_CREATE_FAILED")
            limits = ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise WorkerError("WINDOWS_JOB_CONFIG_FAILED")
            process = self.api.OpenProcess(0x0100 | 0x0001, False, pid)
            if not process or not self.api.AssignProcessToJobObject(self.handle, process):
                raise WorkerError("WINDOWS_JOB_ASSIGN_FAILED")
        except BaseException:
            self.close()
            raise
        finally:
            if process:
                self.api.CloseHandle(process)

    def close(self) -> None:
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


async def kill_process_tree(process: asyncio.subprocess.Process, job: WindowsJob | None = None) -> None:
    if job is not None:
        job.close()
    elif os.name == "nt" and process.returncode is None:
        killer = await asyncio.create_subprocess_exec(
            "taskkill.exe", "/PID", str(process.pid), "/T", "/F",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await killer.wait()
    elif os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.returncode is None:
        try:
            await asyncio.wait_for(process.wait(), timeout=10)
        except TimeoutError:
            process.kill()
            await process.wait()


async def spawn(argv: list[str], cwd: Path) -> tuple[asyncio.subprocess.Process, WindowsJob | None]:
    # The Windows gate waits before launching the actual command, so assigning
    # the job cannot race an immediately-exiting parent or escaped grandchild.
    # os.read consumes exactly one byte, without prefetching the prompt.
    gate = (
        "import os,sys,subprocess\n"
        "if os.read(0,1) != b'1': sys.exit(125)\n"
        "sys.exit(subprocess.call(sys.argv[1:], stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr))\n"
    )
    command = [sys.executable, "-c", gate, *argv] if os.name == "nt" else argv
    flags = {"creationflags": 0x08000200} if os.name == "nt" else {"start_new_session": True}
    process = await asyncio.create_subprocess_exec(
        *command, cwd=cwd, env=child_environment(), stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **flags,
    )
    try:
        job = WindowsJob(process.pid) if os.name == "nt" else None
        if os.name == "nt":
            assert process.stdin is not None
            process.stdin.write(b"1")
            await process.stdin.drain()
        return process, job
    except BaseException:
        await kill_process_tree(process)
        raise


def codex_argv(executable: str, repo: Path, model: str | None) -> list[str]:
    argv = [executable, "-a", "never", "exec", "--json", "--sandbox", "workspace-write",
            "--ignore-user-config", "-C", str(repo)]
    if os.name == "nt":
        # Ignoring user config also removes Windows sandbox setup. Explicitly
        # select the restricted-token sandbox so workspace-write stays writable.
        argv.extend(["-c", 'windows.sandbox="unelevated"'])
    if model:
        argv.extend(["--model", model])
    return [*argv, "-"]


def task_prompt(task: dict[str, Any]) -> str:
    tests = json.dumps(task.get("test_command"), ensure_ascii=False)
    return (
        "Work only inside this task's repository. Read the existing code, implement the requested "
        "change, and actually execute the relevant tests. Do not change .git, .codex, or .env files. "
        "Do not access credentials or files outside this workspace, publish code, or push commits. "
        "The backend independently runs the required test command in a separate Linux container; "
        "your final response is a summary, not the verification result. Describe changed files and "
        "the exact commands you ran, including failures or platform limitations.\n\n"
        f"Required independent test command (JSON): {tests}\n\nUser task:\n{task['prompt']}"
    )


class Backend:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def post(self, path: str, body: dict[str, Any], timeout: float = 20) -> Any:  # noqa: ASYNC109 -- HTTP timeout, not task cancellation.
        try:
            response = await self.client.post(API_PREFIX + path, json=body, timeout=timeout)
        except httpx.HTTPError:
            raise WorkerError("BACKEND_TRANSPORT_ERROR") from None
        if response.is_error:
            raise WorkerError(f"BACKEND_HTTP_{response.status_code}")
        try:
            return response.json()
        except ValueError:
            raise WorkerError("BACKEND_INVALID_JSON") from None

    async def source(self, task_id: str, lease: str) -> bytes:
        try:
            async with self.client.stream("GET", API_PREFIX + f"/tasks/{task_id}/source",
                                          headers={"X-Lease-Token": lease}, timeout=300) as response:
                if response.is_error:
                    raise WorkerError(f"BACKEND_HTTP_{response.status_code}")
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_ARCHIVE_BYTES:
                        raise WorkerError("SOURCE_ARCHIVE_TOO_LARGE")
                return bytes(data)
        except httpx.HTTPError:
            raise WorkerError("BACKEND_TRANSPORT_ERROR") from None


class EventJournal:
    def __init__(self, path: Path, secrets: tuple[str, ...]):
        self.file = path.open("a", encoding="utf-8")
        self.secrets = secrets
        self.sequence = 0
        self.pending: deque[dict[str, Any]] = deque()
        self.session_id: str | None = None
        self.summary = ""
        self.usage: dict[str, Any] = {}
        self.completed = False
        self.failed = False

    def append(self, event: dict[str, Any]) -> None:
        event = redact(event, self.secrets)
        self.sequence += 1
        envelope = {"sequence": self.sequence, "timestamp": datetime.now(UTC).isoformat(), "event": event}
        self.file.write(json.dumps(envelope, ensure_ascii=False) + "\n")
        self.file.flush()
        self.pending.append(envelope)
        if len(self.pending) > 10_000:
            raise WorkerError("EVENT_UPLOAD_BACKLOG_EXCEEDED")
        if event.get("type") == "thread.started":
            self.session_id = event.get("thread_id")
        elif event.get("type") == "turn.completed":
            self.completed = True
            self.usage = event.get("usage") or {}
        elif event.get("type") == "turn.failed":
            self.failed = True
        if event.get("type") == "item.completed" and event.get("item", {}).get("type") == "agent_message":
            self.summary = str(event["item"].get("text", ""))[-16_000:]

    async def upload(self, backend: Backend, task_id: str, lease: str) -> None:
        while self.pending:
            batch, size = [], 2
            for envelope in self.pending:
                event = envelope["event"]
                serialized = json.dumps(event, ensure_ascii=False)
                if len(serialized) > 60_000:
                    event = {"type": str(event.get("type", "event"))[:120], "truncated": True,
                             "preview": serialized[:60_000]}
                outgoing = {**envelope, "event": event}
                entry_size = len(json.dumps(outgoing)) + 2
                if batch and (len(batch) >= 100 or size + entry_size > 480_000):
                    break
                batch.append(outgoing)
                size += entry_size
            await backend.post(f"/tasks/{task_id}/events", {"lease_token": lease, "events": batch})
            for _ in batch:
                self.pending.popleft()

    def close(self) -> None:
        self.file.close()


async def read_events(stream: asyncio.StreamReader, journal: EventJournal) -> None:
    buffer = bytearray()
    discarded = 0

    def consume(line: bytes, dropped: int = 0) -> None:
        if dropped or len(line) > MAX_EVENT_BYTES:
            journal.append({"type": "worker.event_truncated", "bytes": len(line) + dropped})
            return
        if not line.strip():
            return
        try:
            event = json.loads(line)
            if not isinstance(event, dict):
                raise ValueError
        except (ValueError, UnicodeDecodeError):
            event = {"type": "worker.stdout", "text": line.decode("utf-8", errors="replace")[:4096]}
        journal.append(event)

    while chunk := await stream.read(64 * 1024):
        buffer.extend(chunk)
        while b"\n" in buffer:
            line, _, remainder = buffer.partition(b"\n")
            consume(bytes(line), discarded)
            discarded = 0
            buffer = bytearray(remainder)
        if len(buffer) > MAX_EVENT_BYTES:
            discarded += len(buffer)
            buffer.clear()
    if buffer or discarded:
        consume(bytes(buffer), discarded)


async def read_stderr(stream: asyncio.StreamReader, path: Path, secrets: tuple[str, ...]) -> None:
    data = bytearray()
    truncated = False
    try:
        while chunk := await stream.read(8192):
            room = MAX_STDERR_BYTES - len(data)
            data.extend(chunk[:room])
            truncated |= len(chunk) > room
    finally:
        text = redact(data.decode("utf-8", errors="replace"), secrets)
        if truncated:
            text += "\n[stderr truncated]\n"
        await asyncio.to_thread(path.write_text, text, encoding="utf-8")


class CodingJob:
    def __init__(self, backend: Backend, task: dict[str, Any], executor_id: str,
                 workspaces: Path, codex: str, auth_token: str):
        self.backend, self.task, self.executor_id = backend, task, executor_id
        self.task_id = str(task["id"])
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", self.task_id):
            raise WorkerError("INVALID_TASK_ID")
        self.lease = task["lease_token"]
        self.directory = workspaces / f"{self.task_id}-{uuid4().hex[:12]}"
        self.repo = self.directory / "repo"
        self.codex = codex
        self.secrets = (auth_token, self.lease)
        self.abort = asyncio.Event()
        self.reason: str | None = None
        self.journal: EventJournal | None = None

    def cancel(self, reason: str = "CANCELLED") -> None:
        if self.reason is None:
            self.reason = reason
        self.abort.set()

    async def heartbeat(self) -> None:
        last_success = time.monotonic()
        while True:
            try:
                result = await self.backend.post("/executors/heartbeat", {
                    "executor_id": self.executor_id, "task_id": self.task_id, "lease_token": self.lease,
                }, timeout=10)
                last_success = time.monotonic()
                if result.get("cancel_requested"):
                    self.cancel()
            except WorkerError as error:
                if str(error) in {"BACKEND_HTTP_401", "BACKEND_HTTP_403", "BACKEND_HTTP_404", "BACKEND_HTTP_409"}:
                    self.cancel("LEASE_LOST")
                elif time.monotonic() - last_success > 75:
                    self.cancel("LEASE_HEARTBEAT_UNAVAILABLE")
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)

    async def stream_events(self) -> None:
        assert self.journal is not None
        while True:
            try:
                await self.journal.upload(self.backend, self.task_id, self.lease)
            except WorkerError:
                pass  # Durable local log remains; heartbeat enforces lease ownership.
            await asyncio.sleep(1)

    async def cancellable(self, awaitable: Any) -> Any:
        action = asyncio.create_task(awaitable)
        cancelled = asyncio.create_task(self.abort.wait())
        try:
            done, _ = await asyncio.wait({action, cancelled}, return_when=asyncio.FIRST_COMPLETED)
            if cancelled in done:
                raise WorkerError(self.reason or "CANCELLED")
            return await action
        finally:
            for task in (action, cancelled):
                if not task.done():
                    task.cancel()
            await asyncio.gather(action, cancelled, return_exceptions=True)

    async def git(self, *args: str) -> None:
        process, job = await spawn(["git", "-c", "core.longpaths=true", *args], self.repo)
        try:
            _, _ = await asyncio.wait_for(process.communicate(), timeout=30)
            if process.returncode:
                raise WorkerError("GIT_SETUP_FAILED")
        finally:
            await kill_process_tree(process, job)

    async def execute(self) -> int:
        assert self.journal is not None
        process, job = await spawn(codex_argv(self.codex, self.repo, self.task.get("model")), self.repo)
        assert process.stdin and process.stdout and process.stderr
        readers = [asyncio.create_task(read_events(process.stdout, self.journal)),
                   asyncio.create_task(read_stderr(process.stderr, self.directory / "stderr.log", self.secrets))]
        try:
            process.stdin.write(task_prompt(self.task).encode("utf-8"))
            await process.stdin.drain()
            process.stdin.close()
            await asyncio.gather(process.wait(), *readers)
            if process.returncode:
                return process.returncode
            if not self.journal.completed or self.journal.failed:
                raise WorkerError("CODEX_NO_SUCCESSFUL_TURN")
            return 0
        finally:
            await kill_process_tree(process, job)
            await asyncio.gather(*readers, return_exceptions=True)

    async def run(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=False)
        self.journal = EventJournal(self.directory / "events.jsonl", self.secrets)
        heartbeat = asyncio.create_task(self.heartbeat())  # Before downloading/forking the source.
        streaming = asyncio.create_task(self.stream_events())
        exit_code, error, archive = 1, None, empty_archive()
        log("task_started", task_id=self.task_id)
        try:
            try:
                async with asyncio.timeout(float(self.task.get("timeout_seconds", 900))):
                    source = await self.cancellable(self.backend.source(self.task_id, self.lease))
                    await asyncio.to_thread(extract_source, source, self.repo)
                    await self.cancellable(self.git("init", "--quiet", "--template="))
                    await self.cancellable(self.git("add", "--all", "--force"))
                    await self.cancellable(self.git("-c", "user.name=Coding Worker", "-c", "user.email=worker@localhost",
                                                    "-c", "core.hooksPath=", "-c", "commit.gpgsign=false", "commit",
                                                    "--allow-empty", "--quiet", "--no-verify", "-m", "Task source baseline"))
                    exit_code = await self.cancellable(self.execute())
                    if exit_code:
                        error = "CODEX_EXIT_NONZERO"
            except TimeoutError:
                self.cancel("TIMEOUT")
                exit_code, error = 124, "TIMEOUT"
            except WorkerError as exc:
                error = str(exc)
                exit_code = 130 if error == "CANCELLED" else 1
            except asyncio.CancelledError:
                self.cancel("WORKER_STOPPED")
                error, exit_code = "WORKER_STOPPED", 130
            except Exception as exc:
                error, exit_code = "WORKER_" + type(exc).__name__.upper(), 1
            if self.repo.is_dir():
                try:
                    archive = await asyncio.to_thread(pack_workspace, self.repo)
                except Exception as exc:
                    error, exit_code = (str(exc) if isinstance(exc, WorkerError) else "ARCHIVE_FAILED"), 1
            if self.reason and not error:
                error, exit_code = self.reason, 130 if self.reason == "CANCELLED" else 1
            self.journal.append({"type": "worker.finished", "exit_code": exit_code, "error": error})
            streaming.cancel()
            await asyncio.gather(streaming, return_exceptions=True)
            try:
                await self.journal.upload(self.backend, self.task_id, self.lease)
            except WorkerError:
                log("event_upload_incomplete", task_id=self.task_id)
            body = {
                "lease_token": self.lease, "exit_code": exit_code, "summary": self.journal.summary,
                "session_id": self.journal.session_id, "usage": self.journal.usage,
                "archive_base64": base64.b64encode(archive).decode("ascii"),
            }
            if error:
                body["error"] = error
            try:
                await self.backend.post(f"/tasks/{self.task_id}/complete", body, timeout=300)
                log("task_reported", task_id=self.task_id, exit_code=exit_code)
            except WorkerError as exc:
                log("completion_report_failed", task_id=self.task_id, error=str(exc))
        finally:
            heartbeat.cancel()
            streaming.cancel()
            await asyncio.gather(heartbeat, streaming, return_exceptions=True)
            self.journal.close()


async def worker(args: argparse.Namespace) -> None:
    auth_token = read_auth_token()
    workspaces = await asyncio.to_thread(Path(args.workspaces).resolve)
    workspaces.mkdir(parents=True, exist_ok=True)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), lambda *_: loop.call_soon_threadsafe(stop.set))
    active: dict[asyncio.Task, CodingJob] = {}
    async with httpx.AsyncClient(base_url=args.base_url.rstrip("/"),
                                 headers={"Authorization": f"Bearer {auth_token}"},
                                 trust_env=False, follow_redirects=False,
                                 limits=httpx.Limits(max_connections=max(20, args.concurrency * 4))) as client:
        backend = Backend(client)

        async def register() -> None:
            while not stop.is_set():
                try:
                    await backend.post("/executors/register", {
                        "executor_id": args.executor_id, "capacity": args.concurrency, "version": VERSION,
                    })
                except WorkerError as exc:
                    log("registration_failed", error=str(exc))
                try:
                    await asyncio.wait_for(stop.wait(), timeout=15)
                except TimeoutError:
                    pass

        registration = asyncio.create_task(register())
        log("worker_started", executor_id=args.executor_id, capacity=args.concurrency, version=VERSION)
        try:
            while not stop.is_set():
                if args.stop_file and await asyncio.to_thread(Path(args.stop_file).is_file):
                    stop.set()
                    break
                for finished in [task for task in active if task.done()]:
                    job = active.pop(finished)
                    if not finished.cancelled() and finished.exception():
                        log("job_failed", task_id=job.task_id, error=type(finished.exception()).__name__)
                if len(active) < args.concurrency:
                    try:
                        payload = await backend.post("/executors/claim", {
                            "executor_id": args.executor_id, "capacity": args.concurrency,
                        })
                        if payload.get("task"):
                            job = CodingJob(backend, payload["task"], args.executor_id, workspaces,
                                            args.codex_executable, auth_token)
                            active[asyncio.create_task(job.run())] = job
                            continue
                    except WorkerError as exc:
                        log("claim_failed", error=str(exc))
                try:
                    await asyncio.wait_for(stop.wait(), timeout=2)
                except TimeoutError:
                    pass
        finally:
            stop.set()
            for job in active.values():
                job.cancel("WORKER_STOPPED")
            await asyncio.gather(*active, return_exceptions=True)
            registration.cancel()
            await asyncio.gather(registration, return_exceptions=True)
            log("worker_stopped", executor_id=args.executor_id)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:2026")
    parser.add_argument("--concurrency", type=int, choices=range(1, 101), default=2, metavar="1..100")
    parser.add_argument("--workspaces", default=str(PROJECT_ROOT / ".coding-workspaces"))
    parser.add_argument("--executor-id", default=f"{socket.gethostname()}-{uuid4().hex[:12]}")
    parser.add_argument("--codex-executable", default="codex")
    parser.add_argument("--stop-file")
    parser.add_argument("--instance-id", default=uuid4().hex)
    args = parser.parse_args(argv)
    url = urlsplit(args.base_url)
    if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
        parser.error("--base-url must be an HTTP(S) service URL without credentials, query, or fragment")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", args.executor_id):
        parser.error("--executor-id contains unsupported characters")
    return args


def main() -> int:
    try:
        asyncio.run(worker(parse_args()))
        return 0
    except WorkerError as exc:
        log("worker_error", error=str(exc))
        return 1
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        log("worker_error", error=type(exc).__name__)
        return 1


if __name__ == "__main__":
    sys.exit(main())

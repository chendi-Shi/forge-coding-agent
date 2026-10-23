"""Single-decision model adapters; tool execution belongs to the Forge runtime.

The Codex adapter is a compatibility bridge, not a native model API. It rejects
observable tool activity but cannot promise that every CLI tool was disabled.
No provider logs prompts, credentials, raw responses, or subprocess stderr.
"""

from __future__ import annotations

import asyncio
import copy
import ctypes
import json
import os
import shutil
import signal
import sys
import tempfile
import time
from collections import deque
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

MAX_REQUEST_BYTES = 256 * 1024
WIRE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["tool", "finish"]},
        "tool": {"type": "string"},
        "arguments_json": {"type": "string"},
        "summary": {"type": "string"},
    },
    "required": ["action", "tool", "arguments_json", "summary"],
    "additionalProperties": False,
}
DISABLED_CODEX_FEATURES = (
    "shell_tool", "shell_snapshot", "apps", "plugins", "hooks", "multi_agent",
    "browser_use", "browser_use_external", "computer_use", "image_generation",
    "view_image", "sleep_tool", "goals", "code_mode", "workspace_dependencies", "skill_search",
)
SINGLE_STEP_INSTRUCTION = (
    "You are the decision component of a separate coding agent runtime. "
    "Return exactly ONE next decision as JSON. Do not execute anything yourself, "
    "call built-in tools, browse, inspect the local filesystem, delegate, or edit files. "
    "All observations you need are supplied in the conversation. The external runtime "
    "will execute the selected tool and send its result in a later request. "
    "For transport, encode the decision's arguments object as a JSON string in the "
    "arguments_json field. Use action, tool, arguments_json, summary and no other fields. "
    "For finish, use tool='' and arguments_json='{}'. The decoded decision must conform "
    "to this runtime JSON Schema: "
)


class ProviderError(Exception):
    """Stable, credential-free error metadata for runtime events and retry policy."""

    def __init__(self, code: str, *, retryable: bool = False, status_code: int | None = None):
        self.code = code
        self.retryable = retryable
        self.status_code = status_code
        super().__init__(code)


class DecisionProvider(Protocol):
    async def decide(self, messages: list[dict], schema: dict, *, model: str | None = None) -> dict: ...


def _json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (ValueError, TypeError, RecursionError):
        raise ProviderError("invalid_request") from None


def _loads(value: str | bytes) -> Any:
    def no_duplicates(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError
            result[key] = item
        return result

    def no_nonfinite(_value):
        raise ValueError

    try:
        return json.loads(value, object_pairs_hook=no_duplicates, parse_constant=no_nonfinite)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ProviderError("invalid_response") from None


def _validator(schema: dict) -> Draft202012Validator:
    if not isinstance(schema, dict):
        raise ProviderError("invalid_schema")

    def local_refs_only(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in {"$ref", "$dynamicRef"} and (
                    not isinstance(value, str) or not value.startswith("#")
                ):
                    raise ProviderError("external_schema_reference")
                local_refs_only(value)
        elif isinstance(node, list):
            for value in node:
                local_refs_only(value)

    try:
        local_refs_only(schema)
        Draft202012Validator.check_schema(schema)
        return Draft202012Validator(schema)
    except (SchemaError, RecursionError):
        raise ProviderError("invalid_schema") from None


def _request(messages: list[dict], schema: dict) -> tuple[list[dict], Draft202012Validator]:
    validator = _validator(schema)
    if not isinstance(messages, list) or not messages:
        raise ProviderError("invalid_request")
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in {"system", "developer", "user", "assistant"}:
            raise ProviderError("invalid_request")
        if not isinstance(message.get("content"), str) or set(message) - {"role", "content"}:
            raise ProviderError("invalid_request")
    prepared = [{"role": "system", "content": SINGLE_STEP_INSTRUCTION + _json(schema)}, *copy.deepcopy(messages)]
    if len(_json(prepared).encode("utf-8")) > MAX_REQUEST_BYTES:
        raise ProviderError("request_too_large")
    return prepared, validator


def _validate_decision(decision: Any, validator: Draft202012Validator) -> dict:
    if not isinstance(decision, dict) or set(decision) != {"action", "tool", "arguments", "summary"}:
        raise ProviderError("invalid_decision")
    if (not isinstance(decision["action"], str) or decision["action"] not in {"tool", "finish"}
            or not isinstance(decision["tool"], str)
            or not isinstance(decision["arguments"], dict) or not isinstance(decision["summary"], str)):
        raise ProviderError("invalid_decision")
    if decision["action"] == "tool" and not decision["tool"].strip():
        raise ProviderError("invalid_decision")
    if decision["action"] == "finish" and (decision["tool"] or decision["arguments"]):
        raise ProviderError("invalid_decision")
    try:
        validator.validate(decision)
    except ValidationError:
        raise ProviderError("schema_validation") from None
    except Exception:
        # Broken local references must not become a raw schema/prompt exception.
        raise ProviderError("invalid_schema") from None
    return copy.deepcopy(decision)


def _decode(text: str, validator: Draft202012Validator, max_output_bytes: int) -> dict:
    if not isinstance(text, str):
        raise ProviderError("invalid_response")
    if len(text.encode("utf-8")) > max_output_bytes:
        raise ProviderError("output_limit")
    wire = _loads(text)
    if not isinstance(wire, dict) or set(wire) != {"action", "tool", "arguments_json", "summary"}:
        raise ProviderError("invalid_response")
    arguments = wire.pop("arguments_json")
    if not isinstance(arguments, str):
        raise ProviderError("invalid_response")
    return _validate_decision({**wire, "arguments": _loads(arguments)}, validator)


def _usage(value: Any) -> dict:
    if not isinstance(value, dict):
        return {}
    names = {"prompt_tokens", "completion_tokens", "total_tokens", "input_tokens", "output_tokens",
             "cached_input_tokens", "cache_write_input_tokens", "reasoning_output_tokens"}
    return {name: count for name, count in value.items()
            if name in names and isinstance(count, int) and not isinstance(count, bool) and count >= 0}


def _result(decision: dict, usage: dict, started: float, provider: str) -> dict:
    return {"decision": decision, "usage": usage,
            "latency_ms": round((time.monotonic() - started) * 1000), "provider": provider}


class ScriptedProvider:
    """Deterministic test fixture; its output is never a model-quality result."""

    def __init__(self, decisions: list[dict]):
        self._decisions = deque(copy.deepcopy(decisions))

    async def decide(self, messages: list[dict], schema: dict, *, model: str | None = None) -> dict:
        started = time.monotonic()
        _, validator = _request(messages, schema)
        if not self._decisions:
            raise ProviderError("script_exhausted")
        decision = _validate_decision(self._decisions.popleft(), validator)
        return _result(decision, {}, started, "scripted_test_only")


class OpenAICompatibleProvider:
    """One Chat Completions request with no executable tools or hidden retries.

    ``base_url`` is the API root (for example .../v1 or .../api/v3), not a model
    endpoint. Set response_format='json_object' explicitly for compatible APIs
    that do not support json_schema; the same local validation still applies.
    """

    def __init__(self, base_url: str, api_key: str | None, model: str | None = None, *,
                 timeout_seconds: float = 90, max_output_tokens: int = 4096,
                 max_output_bytes: int = 65536, max_response_bytes: int = 1024 * 1024,
                 response_format: str = "json_schema", token_parameter: str = "max_completion_tokens",
                 transport: httpx.AsyncBaseTransport | None = None):
        if not isinstance(api_key, str) or not api_key.strip():
            raise ProviderError("missing_api_key")
        try:
            parsed = urlsplit(base_url)
            valid_url = (parsed.scheme == "https" or (
                parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
            )) and parsed.hostname and not (parsed.username or parsed.password or parsed.query or parsed.fragment)
        except ValueError:
            valid_url = False
        if not valid_url or not 0 < timeout_seconds <= 600 or not 1 <= max_output_tokens <= 32768:
            raise ProviderError("invalid_configuration")
        if response_format not in {"json_schema", "json_object"} or not 1024 <= max_output_bytes <= 1024 * 1024:
            raise ProviderError("invalid_configuration")
        if token_parameter not in {"max_tokens", "max_completion_tokens"}:
            raise ProviderError("invalid_configuration")
        if not max_output_bytes <= max_response_bytes <= 4 * 1024 * 1024:
            raise ProviderError("invalid_configuration")
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._api_key = api_key.strip()
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_output_tokens = max_output_tokens
        self.max_output_bytes = max_output_bytes
        self.max_response_bytes = max_response_bytes
        self.response_format = response_format
        self.token_parameter = token_parameter
        self._transport = transport

    async def decide(self, messages: list[dict], schema: dict, *, model: str | None = None) -> dict:
        started = time.monotonic()
        prepared, validator = _request(messages, schema)
        selected_model = model if model is not None else self.model
        if not isinstance(selected_model, str) or not selected_model.strip():
            raise ProviderError("missing_model")
        response_format = {"type": self.response_format}
        if self.response_format == "json_schema":
            response_format["json_schema"] = {"name": "forge_decision", "strict": True, "schema": WIRE_SCHEMA}
        payload = {"model": selected_model, "messages": prepared, "stream": False,
                   self.token_parameter: self.max_output_tokens, "response_format": response_format}
        try:
            async with asyncio.timeout(self.timeout_seconds):
                async with httpx.AsyncClient(timeout=self.timeout_seconds, trust_env=False,
                                             follow_redirects=False, transport=self._transport) as client:
                    async with client.stream("POST", self._url, json=payload,
                                             headers={"Authorization": f"Bearer {self._api_key}"}) as response:
                        status = response.status_code
                        if status != 200:
                            code = ("authentication" if status in {401, 403} else "rate_limit" if status == 429
                                    else "upstream_unavailable" if status >= 500 else "http_rejected")
                            raise ProviderError(code, retryable=status == 429 or status >= 500, status_code=status)
                        data = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(data) + len(chunk) > self.max_response_bytes:
                                raise ProviderError("output_limit")
                            data.extend(chunk)
        except (TimeoutError, httpx.TimeoutException):
            raise ProviderError("timeout", retryable=True) from None
        except httpx.RequestError:
            raise ProviderError("transport", retryable=True) from None
        envelope = _loads(bytes(data))
        try:
            choices = envelope["choices"]
            if len(choices) != 1:
                raise ProviderError("invalid_response")
            choice = choices[0]
            message = choice["message"]
            if message.get("tool_calls") or message.get("function_call"):
                raise ProviderError("tool_activity_rejected")
            if message.get("refusal") or choice.get("finish_reason") == "content_filter":
                raise ProviderError("refused")
            if choice.get("finish_reason") == "length":
                raise ProviderError("output_limit")
            if choice.get("finish_reason") != "stop":
                raise ProviderError("invalid_response")
            decision = _decode(message["content"], validator, self.max_output_bytes)
        except (KeyError, TypeError, IndexError, AttributeError):
            raise ProviderError("invalid_response") from None
        return _result(decision, _usage(envelope.get("usage")), started, "openai_compatible_http")


class _Transcript:
    """Fail closed on unknown CLI events or any observable built-in activity."""

    def __init__(self):
        self.messages: dict[str, str] = {}
        self.usage: dict = {}
        self.started = False
        self.completed = False

    def consume(self, event: Any) -> None:
        if not isinstance(event, dict):
            raise ProviderError("invalid_transcript")
        kind = event.get("type")
        if kind in {"error", "turn.failed"}:
            raise ProviderError("cli_failed")
        if kind == "thread.started" and not self.started:
            return
        if kind == "turn.started" and not self.started:
            self.started = True
            return
        if kind == "turn.completed" and self.started and not self.completed:
            self.completed = True
            self.usage = _usage(event.get("usage"))
            return
        if kind not in {"item.started", "item.updated", "item.completed"} or not self.started or self.completed:
            raise ProviderError("invalid_transcript")
        item = event.get("item")
        if not isinstance(item, dict):
            raise ProviderError("invalid_transcript")
        if item.get("type") == "error":
            raise ProviderError("cli_failed")
        if item.get("type") not in {"agent_message", "reasoning"}:
            raise ProviderError("tool_activity_rejected")
        if item.get("type") == "agent_message" and kind == "item.completed":
            if not isinstance(item.get("text"), str) or not isinstance(item.get("id"), str):
                raise ProviderError("invalid_transcript")
            if item["id"] in self.messages:
                raise ProviderError("invalid_transcript")
            self.messages[item["id"]] = item["text"]

    def final(self) -> str:
        if not self.completed or len(self.messages) != 1:
            raise ProviderError("invalid_transcript")
        return next(iter(self.messages.values()))


def _child_environment() -> dict[str, str]:
    allowed = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "USERPROFILE",
               "HOME", "HOMEDRIVE", "HOMEPATH", "LOCALAPPDATA", "APPDATA", "PROGRAMFILES",
               "PROGRAMFILES(X86)", "PROGRAMDATA", "OS", "USERNAME", "PROCESSOR_ARCHITECTURE",
               "LANG", "LC_ALL", "CODEX_HOME", "TERM"}
    result = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    result.update({"PYTHONIOENCODING": "utf-8", "NO_COLOR": "1", "GIT_TERMINAL_PROMPT": "0"})
    return result


class _WindowsJob:
    """Own all child descendants, including when the CLI exits before a child."""

    def __init__(self, pid: int):
        from ctypes import wintypes

        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class Limits(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", IO),
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
                raise ProviderError("process_isolation")
            limits = Limits()
            limits.BasicLimitInformation.LimitFlags = 0x2000
            if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise ProviderError("process_isolation")
            process = self.api.OpenProcess(0x0100 | 0x0001, False, pid)
            if not process or not self.api.AssignProcessToJobObject(self.handle, process):
                raise ProviderError("process_isolation")
        except BaseException:
            self.close()
            raise
        finally:
            if process:
                self.api.CloseHandle(process)

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


async def _stop(process: asyncio.subprocess.Process, job: _WindowsJob | None):
    if job is not None:
        job.close()
    elif os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    elif process.returncode is None:
        process.kill()  # The Windows gate has not been released if job setup failed.
    if process.returncode is None:
        await process.wait()


class CodexDecisionProvider:
    """Experimental authenticated CLI bridge with transcript rejection, not a pure LLM API."""

    def __init__(self, executable: str = "codex", *, work_dir: str | Path | None = None,
                 timeout_seconds: float = 90, max_output_bytes: int = 65536,
                 max_transcript_bytes: int = 1024 * 1024):
        if not 0 < timeout_seconds <= 600 or not 1024 <= max_output_bytes <= 1024 * 1024:
            raise ProviderError("invalid_configuration")
        if not max_output_bytes <= max_transcript_bytes <= 4 * 1024 * 1024:
            raise ProviderError("invalid_configuration")
        resolved = shutil.which(executable)
        if not resolved:
            raise ProviderError("cli_not_found")
        self.executable = resolved
        self.work_dir = Path(work_dir).resolve() if work_dir else Path(tempfile.gettempdir()) / "forge-decisions"
        self.timeout_seconds = timeout_seconds
        self.max_output_bytes = max_output_bytes
        self.max_transcript_bytes = max_transcript_bytes

    def _argv(self, directory: Path, schema_path: Path, model: str | None) -> list[str]:
        argv = [self.executable, "-a", "never", "exec", "--json", "--ephemeral", "--ignore-user-config",
                "--strict-config", "--sandbox", "read-only", "--skip-git-repo-check", "-C", str(directory),
                "--output-schema", str(schema_path), "-c", 'web_search="disabled"',
                "-c", "project_doc_max_bytes=0", "-c", "mcp_servers={}"]
        if os.name == "nt":
            argv.extend(["-c", 'windows.sandbox="unelevated"'])
        for feature in DISABLED_CODEX_FEATURES:
            argv.extend(["--disable", feature])
        if model is not None:
            if not isinstance(model, str) or not model.strip():
                raise ProviderError("invalid_configuration")
            argv.extend(["--model", model])
        return [*argv, "-"]

    async def decide(self, messages: list[dict], schema: dict, *, model: str | None = None) -> dict:
        started = time.monotonic()
        prepared, validator = _request(messages, schema)
        prompt = ("The following JSON is the complete conversation for this single decision. "
                  "Follow its system instruction. Do not use tools.\n" + _json(prepared)).encode("utf-8")
        directory = self.work_dir / uuid4().hex
        process = None
        job = None
        tasks = []
        transcript = _Transcript()
        try:
            # mkdir uses inherited Windows ACLs. Python 3.14 mkdtemp(mode=700)
            # may prevent the normal restricted CLI sandbox from traversing it.
            directory.mkdir(parents=True, exist_ok=False)
            schema_path = directory / "decision.schema.json"
            schema_path.write_text(_json(WIRE_SCHEMA), encoding="utf-8")
            argv = self._argv(directory, schema_path, model)
            gate = ("import os,sys,subprocess\nif os.read(0,1)!=b'1': sys.exit(125)\n"
                    "sys.exit(subprocess.call(sys.argv[1:],stdin=sys.stdin,stdout=sys.stdout,stderr=sys.stderr))")
            command = [sys.executable, "-c", gate, *argv] if os.name == "nt" else argv
            flags = {"creationflags": 0x08000200} if os.name == "nt" else {"start_new_session": True}
            async with asyncio.timeout(self.timeout_seconds):
                process = await asyncio.create_subprocess_exec(
                    *command, cwd=directory, env=_child_environment(), stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    limit=self.max_transcript_bytes + 1, **flags,
                )
                job = _WindowsJob(process.pid) if os.name == "nt" else None
                assert process.stdin and process.stdout and process.stderr

                async def stdout():
                    count = 0
                    while line := await process.stdout.readline():
                        count += len(line)
                        if count > self.max_transcript_bytes:
                            raise ProviderError("output_limit")
                        transcript.consume(_loads(line))

                async def stderr():
                    count = 0
                    while chunk := await process.stderr.read(8192):
                        count += len(chunk)
                        if count > 65536:
                            raise ProviderError("output_limit")
                        # Deliberately discard raw diagnostics; they may contain credentials.

                tasks = [asyncio.create_task(stdout()), asyncio.create_task(stderr())]
                process.stdin.write((b"1" if os.name == "nt" else b"") + prompt)
                await process.stdin.drain()
                process.stdin.close()
                await asyncio.gather(*tasks)
                await process.wait()
                if process.returncode:
                    raise ProviderError("cli_failed")
                decision = _decode(transcript.final(), validator, self.max_output_bytes)
            return _result(decision, transcript.usage, started, "codex_cli_bridge")
        except TimeoutError:
            raise ProviderError("timeout", retryable=True) from None
        except (OSError, ValueError):
            raise ProviderError("cli_transport") from None
        finally:
            if process:
                await _stop(process, job)
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            # The recursive cleanup is restricted to this call's UUID directory.
            if directory.resolve().parent == self.work_dir.resolve() and not directory.is_symlink():
                shutil.rmtree(directory, ignore_errors=True)

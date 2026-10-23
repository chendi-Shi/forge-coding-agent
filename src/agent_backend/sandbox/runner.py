"""Run a command inside an already restricted, disposable sandbox container."""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import time
from pathlib import Path, PurePosixPath
from typing import Any


def checked_cwd(value: str, root: Path = Path("/workspace")) -> Path:
    if not isinstance(value, str) or "\x00" in value or "\\" in value:
        raise ValueError("Invalid working directory")
    relative = PurePosixPath(value)
    if relative.is_absolute() or any(part in {"..", ".git"} for part in relative.parts):
        raise ValueError("Working directory must be inside the workspace")
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("Symlink working directories are not allowed")
    if not current.is_dir():
        raise ValueError("Working directory does not exist")
    return current


def run_command(arguments: dict[str, Any]) -> dict[str, Any]:
    command, argv = arguments.get("command"), arguments.get("argv")
    if (command is None) == (argv is None):
        raise ValueError("Provide exactly one of command or argv")
    if command is not None:
        if not isinstance(command, str) or not command.strip() or len(command) > 32768 or "\x00" in command:
            raise ValueError("Invalid command")
        argv = ["/bin/bash", "--noprofile", "--norc", "-lc", command]
    elif not isinstance(argv, list) or not argv or len(argv) > 256 or not all(
        isinstance(item, str) and "\x00" not in item and len(item) <= 32768 for item in argv
    ):
        raise ValueError("Invalid argv")
    timeout = arguments.get("timeout_seconds", 60)
    output_limit = arguments.get("max_output_bytes", 65536)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 300:
        raise ValueError("timeout_seconds must be between 0 and 300")
    if isinstance(output_limit, bool) or not isinstance(output_limit, int) or not 1024 <= output_limit <= 1048576:
        raise ValueError("max_output_bytes must be between 1024 and 1048576")
    cwd = checked_cwd(arguments.get("cwd", "."))
    Path("/tmp/home").mkdir(exist_ok=True)
    env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": "/tmp/home", "LANG": "C.UTF-8", "TMPDIR": "/tmp", "CI": "true", "GIT_TERMINAL_PROMPT": "0"}
    started = time.monotonic()
    output = {"stdout": bytearray(), "stderr": bytearray()}
    truncated = timed_out = False
    process = subprocess.Popen(
        argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, start_new_session=True,
    )
    with selectors.DefaultSelector() as selector:
        assert process.stdout and process.stderr
        for name, pipe in (("stdout", process.stdout), ("stderr", process.stderr)):
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_READ, name)
        try:
            while selector.get_map():
                if time.monotonic() - started >= timeout:
                    timed_out = True
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    break
                for key, _ in selector.select(timeout=min(0.1, max(0, timeout - (time.monotonic() - started)))):
                    chunk = os.read(key.fileobj.fileno(), 16384)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    room = max(0, output_limit - sum(len(value) for value in output.values()))
                    output[key.data].extend(chunk[:room])
                    truncated |= len(chunk) > room
            try:
                exit_code = process.wait(timeout=max(0.1, timeout - (time.monotonic() - started)))
            except subprocess.TimeoutExpired:
                timed_out = True
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                exit_code = process.wait(timeout=2)
        finally:
            if process.poll() is None:
                process.kill()
            process.stdout.close()
            process.stderr.close()
    return {
        "exit_code": exit_code, "stdout": output["stdout"].decode("utf-8", errors="replace"),
        "stderr": output["stderr"].decode("utf-8", errors="replace"), "truncated": truncated,
        "duration": round(time.monotonic() - started, 6), "timed_out": timed_out,
    }

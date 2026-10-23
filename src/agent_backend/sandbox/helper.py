"""File operations executed inside the isolated sandbox container, never on the broker.

The broker serializes operations for each workspace. This process accepts one JSON
request on stdin and writes exactly one JSON result to stdout.
"""

from __future__ import annotations

import base64
import binascii
import io
import json
import os
import stat
import subprocess
import sys
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(os.environ.get("SANDBOX_WORKSPACE", "/workspace"))
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024
MAX_FILES = 2000
MAX_OUTPUT = 1024 * 1024
MAX_ARCHIVE_BYTES = MAX_TOTAL_BYTES + MAX_FILES * 1024
MAX_REQUEST_BYTES = MAX_ARCHIVE_BYTES * 2
PROTECTED_NAMES = {".git", ".codex", ".sandbox", ".sandbox-internal"}
ARCHIVE_EXCLUDED_NAMES = {"node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}


class SandboxError(ValueError):
    def __init__(self, message: str, code: str = "invalid_request") -> None:
        super().__init__(message)
        self.code = code


def _protected(name: str) -> bool:
    lowered = name.casefold()
    return lowered in PROTECTED_NAMES or lowered == ".env" or lowered.startswith(".env.")


def _relative(value: Any, *, allow_root: bool = False) -> str:
    if not isinstance(value, str) or "\x00" in value or "\\" in value or ":" in value:
        raise SandboxError("Path must be a relative POSIX path", "unsafe_path")
    if allow_root and value in ("", "."):
        return ""
    parts = value.split("/")
    if not value or value.startswith("/") or any(p in ("", ".", "..") for p in parts):
        raise SandboxError("Absolute paths and traversal are forbidden", "unsafe_path")
    if any(part.endswith((".", " ")) for part in parts):
        raise SandboxError("Ambiguous trailing dots and spaces are forbidden", "unsafe_path")
    if len(value.encode("utf-8")) > 2048 or any(len(part.encode("utf-8")) > 255 for part in parts):
        raise SandboxError("Path exceeds the length limit", "unsafe_path")
    if any(_protected(p) for p in parts):
        raise SandboxError("Reserved metadata and secret paths are not accessible", "unsafe_path")
    return PurePosixPath(*parts).as_posix()


def _is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _inspect(path: Path) -> os.stat_result | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if _is_link(info):
        raise SandboxError("Symbolic links and junctions are forbidden", "unsafe_path")
    if not stat.S_ISREG(info.st_mode) and not stat.S_ISDIR(info.st_mode):
        raise SandboxError("Only regular files and directories are supported", "unsafe_path")
    return info


def _root() -> Path:
    root = Path(ROOT).absolute()
    # Check the root itself and all its ancestors before any resolve/open operation.
    for candidate in (*reversed(root.parents), root):
        info = _inspect(candidate)
        if info is None or not stat.S_ISDIR(info.st_mode):
            raise SandboxError("Workspace directory is unavailable", "workspace_unavailable")
    return root


def _path(value: Any, *, allow_root: bool = False) -> tuple[str, Path]:
    relative = _relative(value, allow_root=allow_root)
    current = _root()
    for part in relative.split("/") if relative else ():
        current = current / part
        _inspect(current)
    return relative, current


def _read_regular(path: Path, limit: int) -> bytes:
    info = _inspect(path)
    if info is None:
        raise SandboxError("File does not exist", "not_found")
    if not stat.S_ISREG(info.st_mode):
        raise SandboxError("Path must identify a regular file")
    # O_NOFOLLOW also closes the final-component symlink race on Linux.
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        return stream.read(limit)


def _scan(
    start: Path, *, reject_nested_git: bool = False, for_archive: bool = False, truncate: bool = False,
) -> list[Path]:
    root = _root()
    result: list[Path] = []
    pending = [start]
    entries = 0
    while pending:
        current = pending.pop()
        for entry in sorted(current.iterdir(), reverse=True):
            entries += 1
            if entries > MAX_FILES * 5:
                raise SandboxError("Workspace contains too many entries", "limit_exceeded")
            if _protected(entry.name):
                if reject_nested_git and entry.name.casefold() == ".git" and entry.parent != root:
                    raise SandboxError("Nested Git metadata is forbidden", "unsafe_path")
                continue
            if for_archive and entry.name.casefold() in ARCHIVE_EXCLUDED_NAMES:
                continue
            info = _inspect(entry)
            if info is None:
                raise SandboxError("Workspace changed during operation", "workspace_changed")
            if stat.S_ISDIR(info.st_mode):
                pending.append(entry)
            else:
                result.append(entry)
                if len(result) > MAX_FILES:
                    if truncate:
                        return sorted(result, key=lambda path: path.relative_to(root).as_posix())
                    raise SandboxError("Workspace contains too many files", "limit_exceeded")
    return sorted(result, key=lambda path: path.relative_to(root).as_posix())


def _output_limit(arguments: dict[str, Any]) -> int:
    value = arguments.get("max_output", arguments.get("maxOutput", MAX_OUTPUT))
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_OUTPUT:
        raise SandboxError(f"max_output must be between 1 and {MAX_OUTPUT}")
    return value


def _text(arguments: dict[str, Any], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str):
        raise SandboxError(f"{key} must be a string")
    return value


def list_files(arguments: dict[str, Any]) -> dict[str, Any]:
    _, path = _path(arguments.get("path", ""), allow_root=True)
    info = _inspect(path)
    if info is None:
        raise SandboxError("Directory does not exist", "not_found")
    if not stat.S_ISDIR(info.st_mode):
        raise SandboxError("Path must identify a directory")
    files = [entry.relative_to(_root()).as_posix() for entry in _scan(path, truncate=True)]
    max_files = arguments.get("max_files", MAX_FILES)
    if isinstance(max_files, bool) or not isinstance(max_files, int) or not 1 <= max_files <= MAX_FILES:
        raise SandboxError(f"max_files must be between 1 and {MAX_FILES}")
    return {"files": files[:max_files], "truncated": len(files) > max_files}


def read_file(arguments: dict[str, Any]) -> dict[str, Any]:
    relative, path = _path(arguments.get("path"))
    limit = _output_limit(arguments)
    data = _read_regular(path, limit + 1)
    truncated = len(data) > limit
    # Truncating a valid UTF-8 file halfway through a code point is permitted.
    content = data[:limit].decode("utf-8", errors="replace")
    return {"path": relative, "content": content, "truncated": truncated}


def _write(path: Path, data: bytes) -> None:
    if len(data) > MAX_FILE_BYTES:
        raise SandboxError("File exceeds the 2 MiB limit", "limit_exceeded")
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o644)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)


def write_file(arguments: dict[str, Any]) -> dict[str, Any]:
    relative, path = _path(arguments.get("path"))
    data = _text(arguments, "content").encode("utf-8")
    _write(path, data)
    return {"path": relative, "bytes": len(data)}


def replace_text(arguments: dict[str, Any]) -> dict[str, Any]:
    relative, path = _path(arguments.get("path"))
    old = arguments.get("old_text", arguments.get("old"))
    new = arguments.get("new_text", arguments.get("new"))
    if not isinstance(old, str) or not old or not isinstance(new, str):
        raise SandboxError("old_text must be nonempty and new_text must be a string")
    data = _read_regular(path, MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise SandboxError("File exceeds the 2 MiB limit", "limit_exceeded")
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SandboxError("replace_text requires a UTF-8 file") from error
    matches = content.count(old)
    if not matches:
        raise SandboxError("old_text was not found", "not_found")
    replace_all = arguments.get("replace_all", False)
    if not isinstance(replace_all, bool):
        raise SandboxError("replace_all must be a boolean")
    if matches > 1 and not replace_all:
        raise SandboxError("old_text is ambiguous; provide more context or set replace_all")
    updated = content.replace(old, new).encode("utf-8")
    _write(path, updated)
    return {"path": relative, "replacements": matches, "bytes": len(updated)}


def delete_file(arguments: dict[str, Any]) -> dict[str, Any]:
    relative, path = _path(arguments.get("path"))
    info = _inspect(path)
    if info is None:
        raise SandboxError("File does not exist", "not_found")
    if not stat.S_ISREG(info.st_mode):
        raise SandboxError("delete_file only removes regular files")
    path.unlink()
    return {"path": relative, "deleted": True}


def _validate_files(files: list[tuple[str, bytes]]) -> list[tuple[str, bytes]]:
    if len(files) > MAX_FILES:
        raise SandboxError("Project exceeds the 2000 file limit", "limit_exceeded")
    names: set[str] = set()
    total = 0
    for name, data in files:
        _relative(name)
        if name in names:
            raise SandboxError("Duplicate archive or file path")
        names.add(name)
        if len(data) > MAX_FILE_BYTES:
            raise SandboxError("File exceeds the 2 MiB limit", "limit_exceeded")
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise SandboxError("Project exceeds the 20 MiB limit", "limit_exceeded")
    for name in names:
        if any(parent.as_posix() in names for parent in PurePosixPath(name).parents):
            raise SandboxError("A file path conflicts with a parent directory")
    return files


def _unpack(encoded: Any) -> list[tuple[str, bytes]]:
    if not isinstance(encoded, str) or len(encoded) > (MAX_ARCHIVE_BYTES + 2) // 3 * 4:
        raise SandboxError("Archive exceeds the size limit", "limit_exceeded")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as error:
        raise SandboxError("archive_base64 is not valid base64") from error
    files: list[tuple[str, bytes]] = []
    total = 0
    names: set[str] = set()
    directories: set[str] = set()
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_FILES * 2:
                raise SandboxError("Archive has too many entries", "limit_exceeded")
            for entry in entries:
                if entry.orig_filename != entry.filename:
                    raise SandboxError("Archive contains a malformed path", "unsafe_path")
                name = _relative(entry.filename[:-1] if entry.is_dir() else entry.filename)
                if any(part.casefold() in ARCHIVE_EXCLUDED_NAMES for part in PurePosixPath(name).parts):
                    raise SandboxError("Archive contains an excluded dependency or cache path", "unsafe_path")
                mode = entry.external_attr >> 16
                kind = stat.S_IFMT(mode)
                if kind not in (0, stat.S_IFREG, stat.S_IFDIR) or (kind == stat.S_IFDIR and not entry.is_dir()):
                    raise SandboxError("Archive contains a symlink or special file", "unsafe_path")
                if name in names:
                    raise SandboxError("Archive contains duplicate paths")
                names.add(name)
                if entry.is_dir():
                    directories.add(name)
                    continue
                total += entry.file_size
                if entry.file_size > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES or len(files) >= MAX_FILES:
                    raise SandboxError("Archive exceeds the project limits", "limit_exceeded")
                with archive.open(entry) as stream:
                    data = stream.read(MAX_FILE_BYTES + 1)
                if len(data) != entry.file_size:
                    raise SandboxError("Archive file size is inconsistent")
                files.append((name, data))
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError, OSError) as error:
        raise SandboxError("Invalid or unsupported ZIP archive") from error
    file_names = {name for name, _ in files}
    if any(any(parent.as_posix() in file_names for parent in PurePosixPath(name).parents) for name in directories):
        raise SandboxError("Archive file conflicts with a directory")
    return _validate_files(files)


def _safe_git_metadata() -> None:
    git_dir = _root() / ".git"
    info = _inspect(git_dir)
    if info is None or not stat.S_ISDIR(info.st_mode):
        raise SandboxError("Workspace has no Git baseline", "missing_baseline")
    pending = [git_dir]
    entries = 0
    while pending:
        for path in pending.pop().iterdir():
            entries += 1
            if entries > MAX_FILES * 20:
                raise SandboxError("Git metadata exceeds the entry limit", "limit_exceeded")
            info = _inspect(path)
            if info is not None and stat.S_ISDIR(info.st_mode):
                pending.append(path)
    # run_command can edit .git/config: remove filters, includes, and other executable
    # configuration before invoking any Git command. The baseline objects are kept.
    _write(git_dir / "config", b"[core]\n\trepositoryformatversion = 0\n\tbare = false\n\tfilemode = false\n")
    # Alternate object stores could otherwise cause reads outside the workspace.
    if (git_dir / "commondir").exists():
        raise SandboxError("External Git common directories are forbidden", "unsafe_path")
    for filename in ("alternates", "http-alternates"):
        if (git_dir / "objects" / "info" / filename).exists():
            raise SandboxError("Alternate Git object stores are forbidden", "unsafe_path")


def _git(*arguments: str) -> bytes:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update({
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_LFS_SKIP_SMUDGE": "1",
    })
    command = [
        "git", "--no-pager", "-c", f"core.hooksPath={os.devnull}",
        "-c", "core.fsmonitor=false", "-c", "core.autocrlf=false",
        "-c", f"core.attributesFile={os.devnull}", "-c", "protocol.allow=never",
        "-c", "user.name=Sandbox", "-c", "user.email=sandbox@localhost",
        "-c", "commit.gpgSign=false", "-c", "core.pager=cat",
        "-c", "core.quotePath=true", *arguments,
    ]
    try:
        result = subprocess.run(command, cwd=_root(), env=env, capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SandboxError("Git operation could not complete", "git_error") from error
    if result.returncode:
        raise SandboxError("Git operation failed", "git_error")
    return result.stdout


def initialize(arguments: dict[str, Any]) -> dict[str, Any]:
    root = _root()
    if any(root.iterdir()):
        raise SandboxError("Initialization requires an empty workspace", "workspace_not_empty")
    if "archive_base64" in arguments:
        if "files" in arguments:
            raise SandboxError("Specify files or archive_base64, not both")
        files = _unpack(arguments["archive_base64"])
    else:
        supplied = arguments.get("files", [])
        if not isinstance(supplied, list) or len(supplied) > MAX_FILES:
            raise SandboxError("files must be a list of at most 2000 files")
        files = []
        for item in supplied:
            if not isinstance(item, dict):
                raise SandboxError("Each file must contain path and content")
            files.append((_relative(item.get("path")), _text(item, "content").encode("utf-8")))
        files = _validate_files(files)
    for name, data in files:
        _, destination = _path(name)
        _write(destination, data)
    _git("init", "--quiet", "--template=", "--initial-branch=main")
    _safe_git_metadata()
    _git("add", "--all", "--force", "--", ".")
    _git("commit", "--quiet", "--allow-empty", "--no-verify", "-m", "Sandbox baseline")
    baseline = _git("rev-parse", "HEAD").decode("ascii").strip()
    return {"files": len(files), "bytes": sum(len(data) for _, data in files), "baseline": baseline}


def get_diff(arguments: dict[str, Any]) -> dict[str, Any]:
    limit = _output_limit(arguments)
    _scan(_root(), reject_nested_git=True, for_archive=True)
    _safe_git_metadata()
    # Staging all regular files includes additions and deletions without changing
    # HEAD. No other task runs concurrently against this volume.
    # Discard staging left by run_command, then keep reserved files out of both
    # the refreshed index and the returned diff, including nested secret paths.
    paths = ["."]
    for name in sorted(PROTECTED_NAMES | ARCHIVE_EXCLUDED_NAMES | {".env", ".env.*"}):
        paths.extend((f":(exclude,icase){name}", f":(exclude,icase)**/{name}", f":(exclude,icase)**/{name}/**"))
    _git("read-tree", "HEAD")
    _git("add", "--all", "--force", "--", *paths)
    diff = _git("diff", "--cached", "--binary", "--no-ext-diff", "--no-textconv", "--no-renames", "HEAD", "--", *paths)
    return {"diff": diff[:limit].decode("utf-8", errors="replace"), "truncated": len(diff) > limit}


def archive(arguments: dict[str, Any]) -> dict[str, Any]:
    del arguments
    files: list[tuple[str, bytes]] = []
    for path in _scan(_root(), for_archive=True):
        data = _read_regular(path, MAX_FILE_BYTES + 1)
        files.append((path.relative_to(_root()).as_posix(), data))
    _validate_files(files)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
        for name, data in files:
            zipped.writestr(name, data)
    raw = output.getvalue()
    if len(raw) > MAX_ARCHIVE_BYTES:
        raise SandboxError("Archive exceeds the size limit", "limit_exceeded")
    return {"archive_base64": base64.b64encode(raw).decode("ascii"), "bytes": len(raw)}


def restore(arguments: dict[str, Any]) -> dict[str, Any]:
    _output_limit(arguments)
    files = _unpack(arguments.get("archive_base64"))
    # Resolve/check every source, destination and Git metadata before changing any
    # project file. Reserved directories are retained and are never traversed.
    root = _root()
    existing = _scan(root, for_archive=True, reject_nested_git=True)
    new_names = {name for name, _ in files}
    pending = [root]
    while pending:
        for child in pending.pop().iterdir():
            if _protected(child.name) or child.name.casefold() in ARCHIVE_EXCLUDED_NAMES:
                # A ZIP file cannot replace a directory containing preserved data.
                if any(parent.relative_to(root).as_posix() in new_names for parent in child.parents if parent != root and root in parent.parents):
                    raise SandboxError("Restore conflicts with a preserved directory", "unsafe_path")
                continue
            info = _inspect(child)
            if info is not None and stat.S_ISDIR(info.st_mode):
                pending.append(child)
    _safe_git_metadata()
    _git("rev-parse", "--verify", "HEAD^{commit}")
    for path in existing:
        path.unlink()
    # Remove only empty project directories, allowing file/directory role changes
    # while retaining excluded dependencies, secrets, and internal metadata.
    def prune(directory: Path) -> None:
        for child in list(directory.iterdir()):
            if _protected(child.name) or child.name.casefold() in ARCHIVE_EXCLUDED_NAMES:
                continue
            info = _inspect(child)
            if info is not None and stat.S_ISDIR(info.st_mode):
                prune(child)
                if not any(child.iterdir()):
                    child.rmdir()

    prune(root)
    for name, data in files:
        _, destination = _path(name)
        _write(destination, data)
    return {"files": len(files), "bytes": sum(len(data) for _, data in files), **get_diff(arguments)}


TOOLS = {function.__name__: function for function in (
    list_files, read_file, write_file, replace_text, delete_file, get_diff, initialize, archive, restore,
)}


def dispatch(request: Any) -> dict[str, Any]:
    if not isinstance(request, dict) or not isinstance(request.get("name"), str):
        raise SandboxError("Request must contain a tool name")
    arguments = request.get("arguments", {})
    if not isinstance(arguments, dict):
        raise SandboxError("arguments must be an object")
    tool = TOOLS.get(request["name"])
    if tool is None:
        raise SandboxError("Unknown sandbox tool")
    return tool(arguments)


def main() -> int:
    try:
        raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise SandboxError("Request exceeds the size limit", "limit_exceeded")
        result = dispatch(json.loads(raw))
        status = 0
    except SandboxError as error:
        result = {"error": {"code": error.code, "message": str(error)}}
        status = 1
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
        result = {"error": {"code": "invalid_request", "message": "Invalid JSON request"}}
        status = 1
    except Exception:
        # Do not disclose host paths or dump tracebacks into the JSON protocol.
        result = {"error": {"code": "operation_failed", "message": "Sandbox operation failed"}}
        status = 1
    sys.stdout.write(json.dumps(result, ensure_ascii=True, separators=(",", ":")) + "\n")
    return status


if __name__ == "__main__":
    raise SystemExit(main())

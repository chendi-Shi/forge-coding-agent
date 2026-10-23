import base64
import io
import json
import os
import stat
import subprocess
import sys
import zipfile

import pytest

from agent_backend.sandbox import helper


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    path = tmp_path / "workspace"
    path.mkdir()
    monkeypatch.setattr(helper, "ROOT", path)
    return path


def call(name, **arguments):
    return helper.dispatch({"name": name, "arguments": arguments})


def zip_data(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def test_crud_and_diff_include_added_deleted_and_modified(workspace):
    initialized = call("initialize", files=[
        {"path": "old.txt", "content": "old\n"},
        {"path": "source/main.py", "content": "print('before')\n"},
    ])
    assert initialized["files"] == 2
    assert len(initialized["baseline"]) == 40
    assert call("get_diff")["diff"] == ""
    assert call("list_files")["files"] == ["old.txt", "source/main.py"]
    call("write_file", path="new.txt", content="new\n")
    call("replace_text", path="source/main.py", old_text="before", new_text="after")
    assert call("read_file", path="source/main.py")["content"] == "print('after')\n"
    assert call("delete_file", path="old.txt")["deleted"]
    diff = call("get_diff")["diff"]
    assert "new file mode" in diff and "+new" in diff
    assert "deleted file mode" in diff and "-old" in diff
    assert "-print('before')" in diff and "+print('after')" in diff
    assert call("get_diff")["diff"] == diff


@pytest.mark.parametrize("path", ["../escape", "a/../../escape", "/etc/passwd", "C:/temp/escape", "a\\..\\escape", ".git/config", "a/.GIT/config", ".git./config", ".env", "a/.env.local", ".codex/key", "a/./b", "a//b", ""])
def test_tool_paths_are_rejected_without_writes(workspace, path):
    with pytest.raises(helper.SandboxError):
        call("write_file", path=path, content="unsafe")
    assert list(workspace.iterdir()) == []


@pytest.mark.parametrize("path", ["../escape", "/absolute", "C:/escape", "a/../../escape", ".git/config", "dir/.git/config", "a\\..\\escape"])
def test_zip_paths_are_validated_before_extracting(workspace, path):
    encoded = zip_data([("safe.txt", "safe"), (path, "unsafe")])
    with pytest.raises(helper.SandboxError):
        call("initialize", archive_base64=encoded)
    assert list(workspace.iterdir()) == []


@pytest.mark.parametrize("file_type", [stat.S_IFLNK, stat.S_IFIFO, stat.S_IFCHR])
def test_zip_symlink_and_special_files_rejected(workspace, file_type):
    entry = zipfile.ZipInfo("danger")
    entry.create_system = 3
    entry.external_attr = (file_type | 0o777) << 16
    with pytest.raises(helper.SandboxError, match="symlink or special"):
        call("initialize", archive_base64=zip_data([("safe", "safe"), (entry, "../outside")]))
    assert list(workspace.iterdir()) == []


def test_archive_limits_and_path_conflicts_are_atomic(workspace, monkeypatch):
    monkeypatch.setattr(helper, "MAX_FILE_BYTES", 4)
    with pytest.raises(helper.SandboxError, match="limits"):
        call("initialize", archive_base64=zip_data([("safe", "a"), ("large", "12345")]))
    assert list(workspace.iterdir()) == []
    with pytest.raises(helper.SandboxError, match="conflicts"):
        call("initialize", archive_base64=zip_data([("a", "x"), ("a/b", "y")]))
    assert list(workspace.iterdir()) == []


def test_json_initialization_limits_and_conflicts_are_atomic(workspace, monkeypatch):
    monkeypatch.setattr(helper, "MAX_TOTAL_BYTES", 4)
    with pytest.raises(helper.SandboxError, match="20 MiB"):
        call("initialize", files=[{"path": "a", "content": "abc"}, {"path": "b", "content": "def"}])
    assert list(workspace.iterdir()) == []
    with pytest.raises(helper.SandboxError, match="Duplicate"):
        call("initialize", files=[{"path": "a", "content": "a"}, {"path": "a", "content": "b"}])


def test_symlink_components_and_archive_are_rejected(workspace, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("secret")
    try:
        (workspace / "link").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Local account cannot create symlinks; Linux container enforces O_NOFOLLOW")
    for name, arguments in [
        ("write_file", {"path": "link/secret", "content": "changed"}),
        ("read_file", {"path": "link/secret"}),
        ("delete_file", {"path": "link/secret"}),
        ("list_files", {}),
        ("archive", {}),
    ]:
        with pytest.raises(helper.SandboxError, match="Symbolic"):
            call(name, **arguments)
    assert (outside / "secret").read_text() == "secret"


def test_snapshot_roundtrip_omits_git_and_resets_fork_baseline(workspace, tmp_path, monkeypatch):
    call("initialize", files=[{"path": "project.txt", "content": "initial"}])
    call("write_file", path="project.txt", content="current")
    (workspace / "binary.bin").write_bytes(b"\x00\xff\x01")
    encoded = call("archive")
    raw = base64.b64decode(encoded["archive_base64"])
    assert encoded["bytes"] == len(raw)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        assert sorted(archive.namelist()) == ["binary.bin", "project.txt"]
    fork = tmp_path / "fork"
    fork.mkdir()
    monkeypatch.setattr(helper, "ROOT", fork)
    call("initialize", archive_base64=encoded["archive_base64"])
    assert call("read_file", path="project.txt")["content"] == "current"
    assert (fork / "binary.bin").read_bytes() == b"\x00\xff\x01"
    assert call("get_diff")["diff"] == ""


def test_diff_and_read_output_truncation(workspace):
    call("initialize", files=[])
    call("write_file", path="new.txt", content="abcdefgh")
    assert call("read_file", path="new.txt", max_output=3) == {"path": "new.txt", "content": "abc", "truncated": True}
    result = call("get_diff", maxOutput=20)
    assert result["truncated"] and len(result["diff"]) == 20
    with pytest.raises(helper.SandboxError):
        call("get_diff", max_output=helper.MAX_OUTPUT + 1)


def test_binary_diff_contains_a_complete_git_binary_patch(workspace):
    call("initialize", archive_base64=zip_data([("image.bin", b"\x00\xffold")]))
    (workspace / "image.bin").write_bytes(b"\x00\xffnew")
    result = call("get_diff")
    assert not result["truncated"]
    assert "GIT binary patch" in result["diff"]
    assert "literal" in result["diff"]


def test_listing_is_truncated_when_file_count_exceeds_limit(workspace, monkeypatch):
    for name in ("a", "b", "c", "d"):
        (workspace / name).write_text(name)
    monkeypatch.setattr(helper, "MAX_FILES", 2)
    result = call("list_files")
    assert len(result["files"]) == 2 and result["truncated"]


def test_too_long_initialize_path_is_rejected_before_writes(workspace):
    with pytest.raises(helper.SandboxError, match="length"):
        call("initialize", files=[{"path": "safe", "content": "safe"}, {"path": "x" * 256, "content": "too long"}])
    assert list(workspace.iterdir()) == []


def test_replace_text_requires_unambiguous_match(workspace):
    call("write_file", path="a", content="same same")
    with pytest.raises(helper.SandboxError, match="ambiguous"):
        call("replace_text", path="a", old_text="same", new_text="new")
    assert call("read_file", path="a")["content"] == "same same"
    assert call("replace_text", path="a", old="same", new="new", replace_all=True)["replacements"] == 2


def test_git_config_filters_hooks_and_external_diff_cannot_execute(workspace):
    call("initialize", files=[{"path": "a.txt", "content": "before"}])
    marker = workspace / "executed"
    config = workspace / ".git" / "config"
    config.write_text(config.read_text() + '\n[filter "evil"]\n clean = "touch executed"\n required = true\n[diff]\n external = "touch executed"\n[core]\n fsmonitor = "touch executed"\n')
    (workspace / ".gitattributes").write_text("*.txt filter=evil diff=evil\n")
    (workspace / ".git" / "hooks").mkdir(exist_ok=True)
    (workspace / ".git" / "hooks" / "pre-commit").write_text("#!/bin/sh\ntouch executed\n")
    call("write_file", path="a.txt", content="after")
    assert "+after" in call("get_diff")["diff"]
    assert not marker.exists()
    assert "evil" not in config.read_text()


def test_git_alternate_object_store_is_rejected(workspace):
    call("initialize", files=[])
    (workspace / ".git" / "objects" / "info" / "alternates").write_text("/outside/objects\n")
    with pytest.raises(helper.SandboxError, match="Alternate"):
        call("get_diff")


def test_restore_retains_original_baseline_and_removes_absent_files(workspace):
    baseline = call("initialize", files=[{"path": "old.txt", "content": "original"}])["baseline"]
    call("write_file", path="changed.txt", content="changed")
    (workspace / ".env").write_text("SECRET=hidden")
    (workspace / "node_modules").mkdir()
    (workspace / "node_modules" / "dependency").write_text("dependency")
    response = call("restore", archive_base64=zip_data([("new.txt", "restored")]))
    assert response["files"] == 1
    assert not (workspace / "old.txt").exists()
    assert not (workspace / "changed.txt").exists()
    assert (workspace / "new.txt").read_text() == "restored"
    assert (workspace / ".env").read_text() == "SECRET=hidden"
    assert (workspace / "node_modules" / "dependency").exists()
    assert helper._git("rev-parse", "HEAD").decode().strip() == baseline
    assert "deleted file mode" in response["diff"] and "+restored" in response["diff"]
    assert "SECRET" not in response["diff"]


def test_restore_invalid_archive_never_removes_existing_files(workspace):
    call("initialize", files=[{"path": "keep.txt", "content": "original"}])
    with pytest.raises(helper.SandboxError):
        call("restore", archive_base64=zip_data([("good.txt", "new"), ("../bad", "bad")]))
    assert (workspace / "keep.txt").read_text() == "original"
    assert not (workspace / "good.txt").exists()


def test_restore_supports_file_directory_role_changes(workspace):
    call("initialize", files=[{"path": "a", "content": "file"}, {"path": "b/child", "content": "nested"}])
    call("restore", archive_base64=zip_data([("a/child", "nested"), ("b", "file")]))
    assert (workspace / "a" / "child").read_text() == "nested"
    assert (workspace / "b").read_text() == "file"


def test_restore_preserved_directory_conflict_is_atomic(workspace):
    call("initialize", files=[{"path": "keep", "content": "original"}])
    (workspace / "a" / "node_modules").mkdir(parents=True)
    (workspace / "a" / "node_modules" / "dependency").write_text("dependency")
    with pytest.raises(helper.SandboxError, match="preserved"):
        call("restore", archive_base64=zip_data([("a", "file")]))
    assert (workspace / "keep").read_text() == "original"


def test_restore_invalid_output_limit_is_atomic(workspace):
    call("initialize", files=[{"path": "keep", "content": "original"}])
    with pytest.raises(helper.SandboxError, match="max_output"):
        call("restore", archive_base64=zip_data([("new", "file")]), max_output=-1)
    assert (workspace / "keep").read_text() == "original"


def test_archive_and_diff_exclude_sensitive_paths(workspace):
    call("initialize", files=[])
    (workspace / ".env").write_text("SECRET=hidden")
    (workspace / ".codex").mkdir()
    (workspace / ".codex" / "credentials").write_text("hidden")
    (workspace / "node_modules").mkdir()
    (workspace / "node_modules" / "large").write_text("dependency")
    (workspace / "nested").mkdir()
    (workspace / "nested" / ".env.prod").write_text("hidden")
    call("write_file", path="visible.txt", content="visible")
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(call("archive")["archive_base64"]))) as archive:
        assert archive.namelist() == ["visible.txt"]
    diff = call("get_diff")["diff"]
    assert "hidden" not in diff and "+visible" in diff


def test_cli_protocol_is_one_json_object_on_success_and_error(workspace):
    env = dict(os.environ, SANDBOX_WORKSPACE=str(workspace))
    for request, status in [({"name": "list_files", "arguments": {}}, 0), ({"name": "run_command"}, 1)]:
        result = subprocess.run([sys.executable, str(helper.__file__)], input=json.dumps(request), text=True, capture_output=True, env=env, check=False)
        assert result.returncode == status
        assert result.stderr == ""
        response = json.loads(result.stdout)
        assert ("error" in response) == bool(status)

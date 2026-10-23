"""Worker contract, hostile archives, credential handling, and real child cleanup."""

import asyncio
import base64
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
import zipfile
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest.mock import patch

import httpx

_SPEC = spec_from_file_location("coding_worker", Path(__file__).resolve().parents[1] / "scripts" / "coding_worker.py")
assert _SPEC and _SPEC.loader
coding = module_from_spec(_SPEC)
_SPEC.loader.exec_module(coding)


def source_zip(files=None):
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w") as archive:
        for name, value in (files or {"README.md": "A starter repository.\n"}).items():
            archive.writestr(name, value)
    return result.getvalue()


class WorkerFileTests(unittest.TestCase):
    def test_archive_enforces_broker_file_and_total_limits(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(coding, "MAX_FILE_BYTES", 4):
            root = Path(directory)
            with self.assertRaisesRegex(coding.WorkerError, "SOURCE_SIZE_OR_ENCRYPTION_REJECTED"):
                coding.extract_source(source_zip({"large.txt": "12345"}), root / "source")
            repo = root / "output"
            repo.mkdir()
            (repo / "large.txt").write_text("12345")
            with self.assertRaisesRegex(coding.WorkerError, "OUTPUT_WORKSPACE_TOO_LARGE"):
                coding.pack_workspace(repo)
        with tempfile.TemporaryDirectory() as directory, patch.object(coding, "MAX_SOURCE_BYTES", 6):
            with self.assertRaisesRegex(coding.WorkerError, "SOURCE_SIZE_OR_ENCRYPTION_REJECTED"):
                coding.extract_source(source_zip({"a.txt": "1234", "b.txt": "1234"}), Path(directory) / "repo")

    def test_auth_uses_explicit_token_or_named_local_identity(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            env = Path(directory) / ".env"
            env.write_text('API_KEYS_JSON=\'{"isolation-test":"other-user","local-client":"right-user"}\'\n')
            self.assertEqual(coding.read_auth_token(env), "right-user")
            with patch.dict(os.environ, {"AUTH_TOKEN": "explicit"}):
                self.assertEqual(coding.read_auth_token(env), "explicit")
            env.write_text('API_KEYS_JSON={"another-user":"wrong-user"}')
            with self.assertRaisesRegex(coding.WorkerError, "MISSING_LOCAL_CLIENT_TOKEN"):
                coding.read_auth_token(env)

    def test_child_environment_does_not_leak_backend_or_model_credentials(self):
        with patch.dict(os.environ, {"PATH": "tools", "CODEX_HOME": "login-location", "AUTH_TOKEN": "secret",
                                     "API_KEYS_JSON": "secrets", "OPENAI_API_KEY": "api-secret",
                                     "CODEX_API_KEY": "codex-secret", "DATABASE_URL": "db-secret"}, clear=True):
            env = coding.child_environment()
        self.assertEqual(env["CODEX_HOME"], "login-location")
        self.assertEqual(env["PATH"], "tools")
        for key in ("AUTH_TOKEN", "API_KEYS_JSON", "OPENAI_API_KEY", "CODEX_API_KEY", "DATABASE_URL"):
            self.assertNotIn(key, env)

    def test_argv_stdin_and_model_are_not_shell_interpolation(self):
        repo = Path("project with spaces")
        args = coding.codex_argv("codex", repo, None)
        self.assertEqual(args[:4], ["codex", "-a", "never", "exec"])
        self.assertEqual(args[-1], "-")
        self.assertNotIn("--model", args)
        self.assertIn("--ignore-user-config", args)
        self.assertNotIn("--last", args)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", args)
        model = 'name; $(do-not-execute) "quoted"'
        self.assertEqual(coding.codex_argv("codex", repo, model)[-3:], ["--model", model, "-"])
        prompt = coding.task_prompt({"prompt": "Fix actual code", "test_command": ["python", "-m", "unittest"]})
        self.assertIn('"python", "-m", "unittest"', prompt)
        self.assertIn("Fix actual code", prompt)

    def test_archive_rejects_traversal_windows_paths_symlinks_and_case_collisions(self):
        for name in ("../escape", "/absolute", "C:/secret", "nested\\..\\escape", "CON", "name. "):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(coding.WorkerError):
                    coding.extract_source(source_zip({name: "bad"}), Path(directory) / "repo")
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(coding.WorkerError, "SOURCE_DUPLICATE_PATH"):
                coding.extract_source(source_zip({"A.py": "a", "a.py": "b"}), Path(directory) / "repo")
        link = zipfile.ZipInfo("link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            archive.writestr(link, "../outside")
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(coding.WorkerError, "SOURCE_SPECIAL_FILE"):
            coding.extract_source(data.getvalue(), Path(directory) / "repo")

    def test_source_and_output_omit_credentials_generated_dependencies_and_hardlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            files = {"src/main.py": "print('safe')", ".git/hooks/pre-commit": "unsafe", ".codex/auth.json": "secret",
                     ".env": "secret", ".env.production": "secret", "node_modules/a/index.js": "large",
                     ".venv/bin/python": "binary"}
            coding.extract_source(source_zip(files), repo)
            for name, content in files.items():
                target = repo / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
            outside = root / "private.txt"
            outside.write_text("outside secret")
            try:
                os.link(outside, repo / "hardlink.txt")
            except OSError:
                pass
            with zipfile.ZipFile(io.BytesIO(coding.pack_workspace(repo))) as result:
                self.assertEqual(result.namelist(), ["src/main.py"])
            with zipfile.ZipFile(io.BytesIO(coding.pack_workspace(root / "absent"))) as result:
                self.assertEqual(result.namelist(), [])


class FakeBackend:
    def __init__(self, cancel=False, source=None):
        self.calls = []
        self.events = {}
        self.completed = None
        self.cancel = cancel
        self.archive = source if source is not None else source_zip()

    async def source(self, task_id, lease):
        self.calls.append(("source", task_id, lease))
        await asyncio.sleep(0)
        return self.archive

    async def post(self, path, body, timeout=20):  # noqa: ASYNC109 -- Mimics HTTP client timeout.
        self.calls.append((path, body, timeout))
        if path.endswith("/heartbeat"):
            return {"cancel_requested": self.cancel}
        if path.endswith("/events"):
            for envelope in body["events"]:
                self.events[envelope["sequence"]] = envelope
        if path.endswith("/complete"):
            self.completed = body
        return {"ok": True}


class WorkerAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_event_batches_fit_api_limit_even_for_unicode_output(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = coding.EventJournal(Path(directory) / "events.jsonl", ())
            for _ in range(3):
                journal.append({"type": "item.completed", "output": "中" * 100_000})
            backend = FakeBackend()
            await journal.upload(backend, "task", "lease")
            batches = [body["events"] for path, body, _ in backend.calls if path.endswith("/events")]
            self.assertEqual(len(batches), 3)
            self.assertTrue(all(len(json.dumps(batch)) <= 512000 for batch in batches))
            self.assertTrue(all(row["event"]["truncated"] for batch in batches for row in batch))
            journal.close()

    async def test_heartbeat_continues_while_backend_verifies_completion(self):
        started, heartbeat_seen, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class VerifyingBackend(FakeBackend):
            async def post(self, path, body, timeout=20):  # noqa: ASYNC109
                if path.endswith("/complete"):
                    started.set()
                    await release.wait()
                elif path.endswith("/heartbeat") and started.is_set():
                    heartbeat_seen.set()
                return await super().post(path, body, timeout)

        with tempfile.TemporaryDirectory() as directory, patch.object(coding, "HEARTBEAT_INTERVAL_SECONDS", 0.02):
            backend = VerifyingBackend(source=b"invalid archive")
            task = {"id": "verify-pending", "lease_token": "lease", "prompt": "task", "timeout_seconds": 30}
            job = coding.CodingJob(backend, task, "executor", Path(directory), "codex", "auth")
            running = asyncio.create_task(job.run())
            try:
                await asyncio.wait_for(started.wait(), 3)
                await asyncio.wait_for(heartbeat_seen.wait(), 3)
                self.assertFalse(running.done())
            finally:
                release.set()
                await running
            self.assertEqual([timeout for path, _, timeout in backend.calls if path.endswith("/complete")], [300])

    async def test_events_preserve_tool_and_terminal_results_redact_tokens_and_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            journal = coding.EventJournal(path, ("backend-secret", "lease-secret"))
            stream = asyncio.StreamReader()
            events = [
                {"type": "thread.started", "thread_id": "session-123"},
                {"type": "item.completed", "item": {"type": "command_execution", "exit_code": 0,
                    "aggregated_output": "backend-secret Bearer abcdefghijk lease-secret"}},
                {"type": "item.completed", "item": {"type": "agent_message", "text": "Changed code"}},
                {"type": "turn.completed", "usage": {"input_tokens": 10}},
            ]
            payload = "\n".join(json.dumps(item) for item in events).encode()
            stream.feed_data(payload[:31])
            stream.feed_data(payload[31:])
            stream.feed_eof()
            await coding.read_events(stream, journal)
            self.assertTrue(journal.completed)
            self.assertEqual(journal.session_id, "session-123")
            self.assertEqual(journal.summary, "Changed code")
            self.assertEqual(journal.usage, {"input_tokens": 10})
            backend = FakeBackend()
            await journal.upload(backend, "task", "lease-secret")
            self.assertEqual(list(backend.events), [1, 2, 3, 4])
            self.assertFalse(journal.pending)
            journal.close()
            text = path.read_text()
            for secret in ("backend-secret", "lease-secret", "abcdefghijk"):
                self.assertNotIn(secret, text)

    async def test_large_stdout_and_stderr_are_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = coding.EventJournal(root / "events.jsonl", ())
            stream = asyncio.StreamReader()
            stream.feed_data(b"x" * (coding.MAX_EVENT_BYTES + 10) + b"\n")
            stream.feed_data(b'{"type":"turn.completed"}\n')
            stream.feed_eof()
            await coding.read_events(stream, journal)
            self.assertEqual(journal.pending[0]["event"]["type"], "worker.event_truncated")
            self.assertTrue(journal.completed)
            journal.close()
            stderr = asyncio.StreamReader()
            stderr.feed_data(b"x" * (coding.MAX_STDERR_BYTES + 100))
            stderr.feed_eof()
            await coding.read_stderr(stderr, root / "stderr.log", ())
            self.assertLess((root / "stderr.log").stat().st_size, coding.MAX_STDERR_BYTES + 100)

    async def test_source_http_requires_lease_and_never_follows_redirects(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, content=source_zip())

        async with httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(handler),
                                     trust_env=False, headers={"Authorization": "Bearer test"}) as client:
            backend = coding.Backend(client)
            await backend.source("task-1", "lease-1")
        self.assertEqual(calls[0].url.path, "/api/coding/tasks/task-1/source")
        self.assertEqual(calls[0].headers["X-Lease-Token"], "lease-1")
        self.assertEqual(calls[0].extensions["timeout"]["read"], 300)

    @unittest.skipUnless(shutil.which("git"), "git is needed for a real isolated baseline")
    async def test_complete_worker_cycle_uses_real_process_and_returns_filtered_code(self):
        program = (
            "import sys,json,pathlib\n"
            "prompt=sys.stdin.read(); assert 'Fix this repository' in prompt\n"
            "pathlib.Path('answer.py').write_text('answer = 42\\n')\n"
            "print(json.dumps({'type':'thread.started','thread_id':'real-child-session'}),flush=True)\n"
            "print(json.dumps({'type':'item.completed','item':{'type':'command_execution','exit_code':0}}),flush=True)\n"
            "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'Changed answer.py'}}),flush=True)\n"
            "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':1}}),flush=True)\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            backend = FakeBackend()
            task = {"id": "task-success", "lease_token": "lease-secret", "prompt": "Fix this repository",
                    "test_command": ["python", "-m", "unittest"], "timeout_seconds": 30}
            job = coding.CodingJob(backend, task, "executor", Path(directory), "codex", "backend-secret")
            with patch.object(coding, "codex_argv", return_value=[sys.executable, "-c", program]):
                await job.run()
            self.assertEqual(backend.calls[0][0], "/executors/heartbeat")
            self.assertEqual(backend.completed["exit_code"], 0)
            self.assertEqual(backend.completed["session_id"], "real-child-session")
            self.assertEqual(backend.completed["summary"], "Changed answer.py")
            with zipfile.ZipFile(io.BytesIO(base64.b64decode(backend.completed["archive_base64"]))) as archive:
                self.assertEqual(set(archive.namelist()), {"README.md", "answer.py"})
            self.assertIn("worker.finished", [item["event"]["type"] for item in backend.events.values()])

    @unittest.skipUnless(shutil.which("git"), "git is needed for a real isolated baseline")
    async def test_cancel_kills_child_and_grandchild_and_reports_failure(self):
        grandchild = "import pathlib,time; time.sleep(2); pathlib.Path('escaped.txt').write_text('escaped')"
        program = (
            "import sys,subprocess,json,time\n"
            "sys.stdin.read()\n"
            f"subprocess.Popen([sys.executable,'-c',{grandchild!r}],stdin=subprocess.DEVNULL)\n"
            "print(json.dumps({'type':'child.ready'}),flush=True)\n"
            "time.sleep(120)\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            backend = FakeBackend()
            task = {"id": "task-cancel", "lease_token": "lease", "prompt": "Cancel test", "timeout_seconds": 30}
            job = coding.CodingJob(backend, task, "executor", Path(directory), "codex", "auth")
            with patch.object(coding, "codex_argv", return_value=[sys.executable, "-c", program]):
                running = asyncio.create_task(job.run())
                try:
                    async with asyncio.timeout(15):
                        while not job.journal or not any(row["event"]["type"] == "child.ready"  # noqa: ASYNC110 -- Observes a real child process log.
                                                       for row in job.journal.pending):
                            await asyncio.sleep(0.01)
                    job.cancel()
                    await asyncio.wait_for(running, timeout=10)
                finally:
                    if not running.done():
                        job.cancel()
                        await running
            self.assertEqual(backend.completed["exit_code"], 130)
            self.assertEqual(backend.completed["error"], "CANCELLED")
            await asyncio.sleep(2.2)
            self.assertFalse((job.repo / "escaped.txt").exists(), "A grandchild survived the task cancellation")

    async def test_download_failure_and_timeout_are_reported_not_retried_as_new_work(self):
        with tempfile.TemporaryDirectory() as directory:
            backend = FakeBackend(source=b"not a zip")
            task = {"id": "task-invalid", "lease_token": "lease", "prompt": "task", "timeout_seconds": 30}
            job = coding.CodingJob(backend, task, "executor", Path(directory), "codex", "auth")
            await job.run()
            self.assertEqual(backend.completed["exit_code"], 1)
            self.assertEqual(backend.completed["error"], "INVALID_SOURCE_ARCHIVE")
            with zipfile.ZipFile(io.BytesIO(base64.b64decode(backend.completed["archive_base64"]))) as archive:
                self.assertEqual(archive.namelist(), [])
        with tempfile.TemporaryDirectory() as directory:
            backend = FakeBackend()
            task = {"id": "task-timeout", "lease_token": "lease", "prompt": "task", "timeout_seconds": 0.02}
            job = coding.CodingJob(backend, task, "executor", Path(directory), "codex", "auth")

            async def blocked_source(*_):
                await asyncio.sleep(30)

            with patch.object(backend, "source", side_effect=blocked_source):
                await job.run()
            self.assertEqual(backend.completed["error"], "TIMEOUT")
            self.assertEqual(backend.completed["exit_code"], 124)

    async def test_heartbeat_cancellation_prevents_engine_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            backend = FakeBackend(cancel=True)
            task = {"id": "task-cancel-early", "lease_token": "lease", "prompt": "task", "timeout_seconds": 30}
            job = coding.CodingJob(backend, task, "executor", Path(directory), "codex", "auth")
            with patch.object(job, "execute") as execute:
                await job.run()
            execute.assert_not_called()
            self.assertEqual(backend.completed["error"], "CANCELLED")


if __name__ == "__main__":
    unittest.main()

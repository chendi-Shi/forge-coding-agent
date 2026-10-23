"""Provider contract checks. HTTP tests use MockTransport, never a paid API."""

import asyncio
import json
import os
import sys

import httpx
import pytest

from agent_backend.forge.providers import (
    CodexDecisionProvider,
    OpenAICompatibleProvider,
    ProviderError,
    ScriptedProvider,
    _child_environment,
    _Transcript,
)

SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"enum": ["tool", "finish"]},
        "tool": {"type": "string", "enum": ["", "read_file"]},
        "arguments": {"type": "object"},
        "summary": {"type": "string", "maxLength": 1000},
    },
    "required": ["action", "tool", "arguments", "summary"],
    "additionalProperties": False,
}
MESSAGES = [{"role": "user", "content": "Return the next decision."}]
FINISH = {"action": "finish", "tool": "", "arguments": {}, "summary": "done"}
READ = {"action": "tool", "tool": "read_file", "arguments": {"path": "main.py"}, "summary": "Read source"}


def wire(decision=None):
    decision = dict(decision or FINISH)
    decision["arguments_json"] = json.dumps(decision.pop("arguments"))
    return json.dumps(decision)


def envelope(content=None, **message_fields):
    return {"choices": [{"message": {"content": content or wire(), **message_fields}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30, "secret": "not returned"}}


def provider(handler, **kwargs):
    return OpenAICompatibleProvider("https://model.example/v1", "test-secret-credential", "test-model",
                                    transport=httpx.MockTransport(handler), **kwargs)


def transcript_events(text=None):
    return [
        {"type": "thread.started", "thread_id": "test-thread"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"id": "message_1", "type": "agent_message", "text": text or wire()}},
        {"type": "turn.completed", "usage": {"input_tokens": 7, "output_tokens": 9}},
    ]


@pytest.mark.asyncio
async def test_scripted_fixture_is_identified_and_consumes_one_decision():
    supplied = [READ, FINISH]
    adapter = ScriptedProvider(supplied)
    supplied[0] = FINISH
    first = await adapter.decide(MESSAGES, SCHEMA)
    assert first["decision"] == READ
    assert first["provider"] == "scripted_test_only"
    assert first["usage"] == {}
    assert (await adapter.decide(MESSAGES, SCHEMA))["decision"] == FINISH
    with pytest.raises(ProviderError, match="script_exhausted"):
        await adapter.decide(MESSAGES, SCHEMA)


@pytest.mark.asyncio
async def test_http_sends_one_tool_free_request_and_decodes_arguments():
    seen = []

    def handler(request):
        seen.append(request)
        assert str(request.url) == "https://model.example/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer test-secret-credential"
        body = json.loads(request.content)
        assert body["model"] == "override"
        assert body["stream"] is False
        assert body["max_completion_tokens"] == 4096
        assert "tools" not in body and "functions" not in body
        assert "test-secret-credential" not in request.content.decode()
        assert body["response_format"]["json_schema"]["strict"] is True
        assert body["messages"][1:] == MESSAGES
        return httpx.Response(200, json=envelope(wire(READ)))

    result = await provider(handler).decide(MESSAGES, SCHEMA, model="override")
    assert result["decision"] == READ and len(seen) == 1
    assert result["usage"] == {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}
    assert result["provider"] == "openai_compatible_http"
    assert result["latency_ms"] >= 0


@pytest.mark.asyncio
async def test_compatible_json_mode_is_explicit_and_still_validates():
    def handler(request):
        body = json.loads(request.content)
        assert body["response_format"] == {"type": "json_object"}
        assert body["max_tokens"] == 4096 and "max_completion_tokens" not in body
        return httpx.Response(200, json=envelope(wire({**READ, "tool": "unknown_tool"})))

    with pytest.raises(ProviderError, match="schema_validation"):
        await provider(handler, response_format="json_object", token_parameter="max_tokens").decide(MESSAGES, SCHEMA)


@pytest.mark.parametrize("status,code,retryable", [
    (401, "authentication", False), (403, "authentication", False), (429, "rate_limit", True),
    (503, "upstream_unavailable", True), (400, "http_rejected", False), (307, "http_rejected", False),
])
@pytest.mark.asyncio
async def test_http_error_codes_never_include_response_or_credentials(status, code, retryable):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, text="test-secret-credential echoed by remote error",
                              headers={"location": "https://elsewhere.example"})

    with pytest.raises(ProviderError) as caught:
        await provider(handler).decide(MESSAGES, SCHEMA)
    assert caught.value.code == code and caught.value.retryable is retryable
    assert caught.value.status_code == status
    assert str(caught.value) == code and len(calls) == 1


@pytest.mark.asyncio
async def test_http_timeout_and_transport_have_distinct_codes():
    async def slow(_request):
        await asyncio.sleep(1)

    with pytest.raises(ProviderError, match="timeout") as caught:
        await provider(slow, timeout_seconds=0.02).decide(MESSAGES, SCHEMA)
    assert caught.value.retryable

    def failed(request):
        raise httpx.ConnectError("contains test-secret-credential", request=request)

    with pytest.raises(ProviderError, match="^transport$"):
        await provider(failed).decide(MESSAGES, SCHEMA)


@pytest.mark.parametrize("bad_response,code", [
    (envelope("not json"), "invalid_response"),
    (envelope('{"action":"finish","action":"tool"}'), "invalid_response"),
    (envelope(wire({**FINISH, "arguments": {"unexpected": True}})), "invalid_decision"),
    (envelope(wire({**FINISH, "action": []})), "invalid_decision"),
    (envelope(tool_calls=[{"id": "tool-attempt"}]), "tool_activity_rejected"),
    (envelope(refusal="private explanation"), "refused"),
    ({"choices": []}, "invalid_response"),
    ({"choices": [{"message": {"content": wire()}, "finish_reason": "length"}]}, "output_limit"),
])
@pytest.mark.asyncio
async def test_invalid_or_native_tool_responses_fail_closed(bad_response, code):
    with pytest.raises(ProviderError, match=f"^{code}$"):
        await provider(lambda _: httpx.Response(200, json=bad_response)).decide(MESSAGES, SCHEMA)


@pytest.mark.asyncio
async def test_response_body_has_a_byte_cap():
    with pytest.raises(ProviderError, match="output_limit"):
        await provider(lambda _: httpx.Response(200, content=b" " * 2048),
                       max_output_bytes=1024, max_response_bytes=1024).decide(MESSAGES, SCHEMA)


def test_configuration_does_not_silently_use_existing_codex_auth_or_insecure_http():
    with pytest.raises(ProviderError, match="missing_api_key"):
        OpenAICompatibleProvider("https://model.example/v1", None)
    with pytest.raises(ProviderError, match="invalid_configuration"):
        OpenAICompatibleProvider("http://remote.example/v1", "test-key")
    with pytest.raises(ProviderError, match="invalid_configuration"):
        OpenAICompatibleProvider("https://user:password@model.example/v1", "test-key")
    OpenAICompatibleProvider("http://127.0.0.1:1234/v1", "local-test-key")


@pytest.mark.asyncio
async def test_schema_remote_references_and_oversized_inputs_are_rejected_before_http():
    def never_called(_request):
        pytest.fail("HTTP must not be called for an invalid request")

    adapter = provider(never_called)
    with pytest.raises(ProviderError, match="external_schema_reference"):
        await adapter.decide(MESSAGES, {"$ref": "https://elsewhere.example/schema"})
    with pytest.raises(ProviderError, match="request_too_large"):
        await adapter.decide([{"role": "user", "content": "x" * (300 * 1024)}], SCHEMA)


@pytest.mark.parametrize("item_type", ["command_execution", "file_change", "mcp_tool_call", "web_search",
                                      "collab_tool_call", "todo_list", "future_unknown_tool"])
def test_transcript_rejects_every_non_message_item_immediately(item_type):
    transcript = _Transcript()
    transcript.consume({"type": "turn.started"})
    with pytest.raises(ProviderError, match="tool_activity_rejected"):
        transcript.consume({"type": "item.started", "item": {"id": "tool_1", "type": item_type}})


def test_transcript_requires_one_completed_message_and_turn():
    transcript = _Transcript()
    for event in transcript_events():
        transcript.consume(event)
    assert transcript.final() == wire()
    assert transcript.usage == {"input_tokens": 7, "output_tokens": 9}
    with pytest.raises(ProviderError, match="invalid_transcript"):
        transcript.consume({"type": "future_new_event"})
    with pytest.raises(ProviderError, match="invalid_transcript"):
        _Transcript().final()


def test_codex_args_restrict_tools_and_environment_drops_backend_keys(tmp_path, monkeypatch):
    adapter = CodexDecisionProvider(sys.executable, work_dir=tmp_path)
    argv = adapter._argv(tmp_path, tmp_path / "schema.json", None)
    assert ["--sandbox", "read-only"] == argv[argv.index("--sandbox"):argv.index("--sandbox") + 2]
    assert "--ignore-user-config" in argv and "--ephemeral" in argv
    assert 'web_search="disabled"' in argv and "shell_tool" in argv and "plugins" in argv
    assert "--model" not in argv and "--dangerously-bypass-approvals-and-sandbox" not in argv
    assert "code_mode_host" not in argv
    monkeypatch.setenv("AUTH_TOKEN", "backend-token")
    monkeypatch.setenv("OPENAI_API_KEY", "model-api-key")
    monkeypatch.setenv("DATABASE_URL", "database-secret")
    child = _child_environment()
    assert not {"AUTH_TOKEN", "OPENAI_API_KEY", "DATABASE_URL"}.intersection(child)


class FakeCLIProvider(CodexDecisionProvider):
    def __init__(self, code, work_dir, **kwargs):
        super().__init__(sys.executable, work_dir=work_dir, **kwargs)
        self.code = code

    def _argv(self, directory, schema_path, model):
        return [sys.executable, "-c", self.code]


@pytest.mark.asyncio
async def test_subprocess_decision_roundtrip_and_temporary_directory_cleanup(tmp_path):
    script = f"import json; events={transcript_events()!r}; [print(json.dumps(e),flush=True) for e in events]"
    result = await FakeCLIProvider(script, tmp_path).decide(MESSAGES, SCHEMA)
    assert result["decision"] == FINISH and result["provider"] == "codex_cli_bridge"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_timeout_kills_descendant_and_cleans_private_workspace(tmp_path):
    marker = tmp_path / "must-not-appear.txt"
    child = f"import time,pathlib; time.sleep(0.8); pathlib.Path({str(marker)!r}).write_text('escaped')"
    script = f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{child!r}]); time.sleep(20)"
    with pytest.raises(ProviderError, match="timeout"):
        await FakeCLIProvider(script, tmp_path / "runs", timeout_seconds=0.15).decide(MESSAGES, SCHEMA)
    await asyncio.sleep(1)
    assert not marker.exists()
    assert list((tmp_path / "runs").iterdir()) == []


@pytest.mark.asyncio
async def test_tool_activity_stops_process_before_final_answer(tmp_path):
    events = [{"type": "turn.started"}, {"type": "item.started", "item": {"type": "command_execution"}}]
    script = (f"import json,time; events={events!r}; "
              "[print(json.dumps(e),flush=True) for e in events]; time.sleep(20)")
    with pytest.raises(ProviderError, match="tool_activity_rejected"):
        await FakeCLIProvider(script, tmp_path, timeout_seconds=3).decide(MESSAGES, SCHEMA)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(os.environ.get("RUN_CODEX_PREFLIGHT") != "1", reason="Explicit real-model opt-in only")
@pytest.mark.asyncio
async def test_real_codex_single_decision_without_observable_tools(tmp_path):
    # Uses the normal CLI login, not an API key or copied credentials.
    adapter = CodexDecisionProvider(timeout_seconds=90)
    result = await adapter.decide([{"role": "user", "content": "Return finish with summary decision-probe-ok."}], SCHEMA)
    assert result["decision"] == {**FINISH, "summary": "decision-probe-ok"}
    assert result["provider"] == "codex_cli_bridge"
    assert result["usage"].get("output_tokens", 0) > 0

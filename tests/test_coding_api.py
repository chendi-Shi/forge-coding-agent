import importlib
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

api = importlib.import_module("agent_backend.coding.api")
client_module = importlib.import_module("agent_backend.coding.broker")


async def test_verification_runs_on_disposable_copy(monkeypatch):
    operations = AsyncMock(return_value={})
    command = AsyncMock(return_value={"exit_code": 0, "stdout": "OK"})
    monkeypatch.setattr(api, "broker", operations)
    monkeypatch.setattr(api, "tool", command)
    result = await api.verify_workspace("original", "python test.py")
    target = operations.call_args_list[0].kwargs["json"]["workspace_id"]
    assert target != "original"
    command.assert_awaited_once_with(target, "run_command", {"command": "python test.py", "timeout_seconds": 120})
    assert operations.call_args_list[-1].args == ("DELETE", f"/workspaces/{target}")
    assert result["exit_code"] == 0


async def test_verification_failure_still_cleans_copy(monkeypatch):
    operations = AsyncMock(return_value={})
    monkeypatch.setattr(api, "broker", operations)
    monkeypatch.setattr(api, "tool", AsyncMock(side_effect=HTTPException(503, "offline")))
    with pytest.raises(HTTPException) as failure:
        await api.verify_workspace("original", "test")
    assert failure.value.status_code == 503
    assert operations.call_args_list[-1].args[0] == "DELETE"


@pytest.mark.parametrize("status", [200, 422])
async def test_broker_error_payload_is_never_success(monkeypatch, status):
    monkeypatch.setenv("BROKER_TOKEN", "test-credential" * 3)
    actual_client = httpx.AsyncClient
    monkeypatch.setattr(client_module.httpx, "AsyncClient", lambda **kwargs: actual_client(
        **kwargs, transport=httpx.MockTransport(lambda request: httpx.Response(status, json={"error": {"code": "unsafe_path"}})),
    ))
    with pytest.raises(HTTPException) as failure:
        await client_module.broker("POST", "/workspaces/test/restore")
    assert failure.value.status_code == 422
    assert "unsafe_path" in failure.value.detail

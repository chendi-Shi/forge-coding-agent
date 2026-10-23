import base64
from unittest.mock import AsyncMock

import httpx
import pytest

from agent_backend.sandbox import server
from agent_backend.sandbox.server import create_app

TOKEN = "test-only-broker-token-not-a-real-secret"
IDENTITY = "d309f8f5-04f1-47fd-bb59-37ff451f3b89"


def manager():
    return AsyncMock()


async def test_all_routes_require_private_bearer_before_touching_docker():
    fake = manager()
    app = create_app(fake, TOKEN)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://broker") as client:
        for method, path in [("GET", "/health"), ("POST", "/workspaces"), ("POST", f"/workspaces/{IDENTITY}/tools"), ("DELETE", f"/workspaces/{IDENTITY}")]:
            response = await client.request(method, path)
            assert response.status_code == 401
            assert TOKEN not in response.text
    assert not fake.mock_calls


async def test_create_and_restore_preserve_the_agreed_contract():
    fake = manager()
    fake.create.return_value = {"workspace_id": IDENTITY, "baseline": "new"}
    fake.tool.return_value = {"files": 1, "bytes": 2, "diff": "diff --git", "truncated": False}
    app = create_app(fake, TOKEN)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://broker", headers={"Authorization": "Bearer " + TOKEN}) as client:
        created = await client.post("/workspaces", json={"workspace_id": IDENTITY, "files": [{"path": "a.py", "content": "42"}]})
        assert created.status_code == 201
        fake.create.assert_awaited_once_with(IDENTITY, {"files": [{"path": "a.py", "content": "42"}]})
        restored = await client.post(f"/workspaces/{IDENTITY}/restore", json={"archive_base64": "emlw"})
        assert restored.status_code == 200 and restored.json()["diff"] == "diff --git"
        fake.tool.assert_awaited_once_with(IDENTITY, "restore", {"archive_base64": "emlw"})


@pytest.mark.parametrize("payload", [{"name": []}, {"name": "initialize"}, {"name": "archive"}, {"name": "restore"}, {"name": "run_command", "arguments": []}, {"name": "get_diff", "HostConfig": {"Privileged": True}}])
async def test_tool_endpoint_rejects_unknown_tools_and_policy_injection(payload):
    fake = manager()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(fake, TOKEN)), base_url="http://broker", headers={"Authorization": "Bearer " + TOKEN}) as client:
        response = await client.post(f"/workspaces/{IDENTITY}/tools", json=payload)
    assert response.status_code == 400
    fake.tool.assert_not_awaited()


async def test_archive_response_is_downloadable_zip():
    fake = manager()
    fake.tool.return_value = {"archive_base64": base64.b64encode(b"zip bytes").decode()}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(fake, TOKEN)), base_url="http://broker", headers={"Authorization": "Bearer " + TOKEN}) as client:
        response = await client.get(f"/workspaces/{IDENTITY}/archive")
    assert response.status_code == 200 and response.content == b"zip bytes"
    assert response.headers["content-type"] == "application/zip"
    fake.tool.assert_awaited_once_with(IDENTITY, "archive", {})


@pytest.mark.parametrize("method,suffix,payload", [
    ("POST", "tools", {"name": "write_file", "arguments": {"path": "../bad", "content": "x"}}),
    ("POST", "restore", {"archive_base64": "invalid"}),
    ("GET", "diff", None), ("GET", "archive", None),
])
async def test_helper_error_envelopes_never_return_http_success(method, suffix, payload):
    fake = manager()
    fake.tool.return_value = {"error": {"code": "unsafe_path", "message": "Rejected path"}}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(fake, TOKEN)), base_url="http://broker", headers={"Authorization": "Bearer " + TOKEN}) as client:
        response = await client.request(method, f"/workspaces/{IDENTITY}/{suffix}", json=payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unsafe_path"


async def test_body_size_limit_is_checked_before_parsing_or_docker(monkeypatch):
    monkeypatch.setattr(server, "MAX_PAYLOAD", 20)
    fake = manager()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(fake, TOKEN)), base_url="http://broker", headers={"Authorization": "Bearer " + TOKEN}) as client:
        response = await client.post("/workspaces", content=b"x" * 21)
    assert response.status_code == 413
    fake.create.assert_not_awaited()


def test_short_or_missing_broker_secret_prevents_startup():
    with pytest.raises(RuntimeError, match="at least 32"):
        create_app(manager(), "short")

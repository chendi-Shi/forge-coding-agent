import json

import httpx
import pytest
from fastapi import FastAPI

from agent_backend.security import BearerGuard
from agent_backend.settings import AgentSettings, get_api_keys, identity_from_header


@pytest.fixture(autouse=True)
def isolated_keys(monkeypatch):
    get_api_keys.cache_clear()
    monkeypatch.setenv("API_KEYS_JSON", json.dumps({"alice": "a" * 40, "bob": "b" * 40}))
    yield
    get_api_keys.cache_clear()


@pytest.mark.parametrize("header", ["", "Bearer wrong", "Basic " + "a" * 40, "Bearer ", "Bearer 中文"])
def test_invalid_credentials_rejected(header):
    assert identity_from_header(header) is None


def test_distinct_credentials_have_distinct_identities():
    assert identity_from_header("Bearer " + "a" * 40) == "alice"
    assert identity_from_header("bearer " + "b" * 40) == "bob"


@pytest.mark.parametrize("raw", ["{}", "not-json", "[]", '{"alice":"short"}'])
def test_malformed_credentials_fail_closed(monkeypatch, raw):
    monkeypatch.setenv("API_KEYS_JSON", raw)
    with pytest.raises(ValueError):
        get_api_keys()
    assert identity_from_header("Bearer " + "a" * 40) is None


def test_duplicate_key_cannot_alias_two_users(monkeypatch):
    monkeypatch.setenv("API_KEYS_JSON", json.dumps({"alice": "a" * 40, "bob": "a" * 40}))
    with pytest.raises(ValueError, match="distinct"):
        get_api_keys()


async def test_guard_protects_business_routes_even_if_upstream_does_not():
    application = FastAPI()
    application.add_middleware(BearerGuard)

    @application.get("/private")
    def private():
        return {"ok": True}

    @application.get("/health")
    def health():
        return {"ok": True}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client:
        assert (await client.get("/health")).status_code == 200
        assert (await client.get("/private")).status_code == 401
        assert (await client.get("/private", headers={"Authorization": "Bearer wrong"})).status_code == 401
        assert (await client.get("/private", headers={"Authorization": "Bearer " + "a" * 40})).status_code == 200


def test_real_model_mode_requires_explicit_configuration(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="requires"):
        AgentSettings(agent_mode="llm", llm_model="").validate_runtime()

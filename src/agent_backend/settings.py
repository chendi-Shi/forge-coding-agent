import json
import os
import secrets
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AgentSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    agent_mode: str = "mock"
    mock_delay_seconds: float = Field(default=10, ge=0, le=60)
    llm_model: str = ""
    llm_timeout_seconds: float = Field(default=60, gt=0, le=300)
    llm_max_retries: int = Field(default=2, ge=0, le=5)
    llm_max_tokens: int = Field(default=2048, ge=1, le=16384)
    worker_count: int = Field(default=5, ge=1)
    n_jobs_per_worker: int = Field(default=10, ge=1)

    def validate_runtime(self) -> None:
        if self.agent_mode not in {"mock", "llm"}:
            raise ValueError("AGENT_MODE must be mock or llm")
        if self.agent_mode == "llm":
            if not self.llm_model.strip() or not os.environ.get("OPENAI_API_KEY", "").strip():
                raise ValueError("llm mode requires LLM_MODEL and OPENAI_API_KEY")


@lru_cache
def get_api_keys() -> dict[str, str]:
    """Identity -> opaque key. Never include values in errors or logs."""
    try:
        data = json.loads(os.environ.get("API_KEYS_JSON", "{}"))
    except (ValueError, TypeError):
        raise ValueError("API_KEYS_JSON must be valid JSON") from None
    if not isinstance(data, dict) or not data:
        raise ValueError("API_KEYS_JSON must contain at least one identity and key")
    if any(
        not isinstance(identity, str) or not identity.strip()
        or not isinstance(token, str) or len(token) < 32 or not token.isascii()
        for identity, token in data.items()
    ):
        raise ValueError("API keys need a nonempty identity and at least 32 ASCII characters")
    if len(set(data.values())) != len(data):
        raise ValueError("API keys must be distinct for each identity")
    return data


def identity_from_header(header: str) -> str | None:
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token or not token.isascii():
        return None
    try:
        keys = get_api_keys()
    except ValueError:
        return None
    for identity, expected in keys.items():
        if secrets.compare_digest(token, expected):
            return identity
    return None

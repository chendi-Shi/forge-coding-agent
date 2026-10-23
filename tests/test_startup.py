import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_broken_upstream_auth_loader_refuses_to_start(tmp_path):
    config = tmp_path / "broken.json"
    config.write_text(json.dumps({"graphs": {}, "auth": {"path": "./missing_auth.py:auth"}}))
    environment = dict(os.environ)
    environment.update({
        "API_KEYS_JSON": json.dumps({"test": "x" * 40}),
        "AEGRA_CONFIG": str(config),
        "AGENT_MODE": "mock",
        "AUTH_TYPE": "custom",
    })
    result = subprocess.run(
        [sys.executable, "-c", "import agent_backend.server"],
        cwd=ROOT, env=environment, capture_output=True, text=True, timeout=45,
    )
    assert result.returncode != 0
    assert "refusing anonymous fallback" in result.stderr


def test_real_aegra_app_imports_and_docs_expose_bearer_auth():
    environment = dict(os.environ)
    environment.update({
        "API_KEYS_JSON": json.dumps({"test": "x" * 40}),
        "AEGRA_CONFIG": str(ROOT / "aegra.json"),
        "AGENT_MODE": "mock",
        "AUTH_TYPE": "custom",
    })
    code = (
        "from agent_backend.server import app; "
        "schema=app.openapi(); "
        "assert schema['components']['securitySchemes']['BearerAuth']['scheme']=='bearer'; "
        "assert '/v1/info' in schema['paths']"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT, env=environment, capture_output=True, text=True, timeout=45,
    )
    assert result.returncode == 0, result.stderr

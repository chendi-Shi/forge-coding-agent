"""Fail-closed ASGI entry point wrapping the pinned Aegra runtime."""

import os
from pathlib import Path

from agent_backend.security import BearerGuard
from agent_backend.settings import AgentSettings, get_api_keys


def create_app():
    # Validate before importing Aegra: empty/malformed keys fail startup.
    get_api_keys()
    config = AgentSettings()
    config.validate_runtime()
    # This project exposes the v1 API. The upstream v2 command envelope has
    # different admission/error semantics and is deliberately not enabled.
    os.environ.setdefault("FF_V2_EVENT_STREAMING", "false")
    from aegra_api.core.auth_middleware import get_auth_instance
    from aegra_api.main import app as aegra_app
    from fastapi.responses import FileResponse, RedirectResponse
    from fastapi.staticfiles import StaticFiles
    from sqlalchemy.exc import IntegrityError

    from agent_backend.coding.api import router as coding_router
    from agent_backend.concurrency import integrity_error_response
    from agent_backend.forge.api import router as forge_router

    backend_auth = get_auth_instance()
    if backend_auth is None or not getattr(backend_auth, "_authenticate_handler", None):
        raise RuntimeError("Aegra authentication did not load; refusing anonymous fallback")
    aegra_app.add_middleware(BearerGuard)
    aegra_app.add_exception_handler(IntegrityError, integrity_error_response)
    aegra_app.include_router(coding_router)
    aegra_app.include_router(forge_router)
    studio = Path(__file__).parent / "studio"
    if studio.is_dir():
        aegra_app.mount("/studio/static", StaticFiles(directory=studio), name="coding-studio")

        @aegra_app.get("/", include_in_schema=False)
        async def home():
            return RedirectResponse("/studio")

        @aegra_app.get("/studio", include_in_schema=False)
        async def coding_studio():
            return FileResponse(studio / "index.html")

    @aegra_app.get("/v1/info", tags=["Application"])
    async def application_info():
        return {
            "mode": config.agent_mode,
            "graph_id": "agent",
            "slots_per_instance": config.worker_count * config.n_jobs_per_worker,
            "capacity_note": "Configured execution slots; actual throughput requires workload measurement.",
        }

    original_openapi = aegra_app.openapi

    def openapi_with_bearer():
        schema = original_openapi()
        schema.setdefault("components", {}).setdefault("securitySchemes", {})["BearerAuth"] = {
            "type": "http", "scheme": "bearer",
        }
        schema["security"] = [{"BearerAuth": []}]
        for path in ("/health", "/ok"):
            for operation in schema.get("paths", {}).get(path, {}).values():
                if isinstance(operation, dict):
                    operation["security"] = []
        return schema

    aegra_app.openapi = openapi_with_bearer
    return aegra_app


app = create_app()

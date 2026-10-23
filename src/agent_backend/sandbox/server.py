"""Private broker API. Run one replica; public API enforces tenant ownership."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import secrets
from contextlib import asynccontextmanager, suppress
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response

from .engine import MAX_PAYLOAD, TOOLS, SandboxError, SandboxManager, engine_client


def create_app(manager: SandboxManager | None = None, token: str | None = None) -> FastAPI:
    configured_token = token if token is not None else os.environ.get("BROKER_TOKEN", "")
    if len(configured_token) < 32:
        raise RuntimeError("BROKER_TOKEN must contain at least 32 characters")
    owns_client = manager is None
    if manager is None:
        concurrency = int(os.environ.get("SANDBOX_CONCURRENCY", "2"))
        if not 1 <= concurrency <= 16:
            raise RuntimeError("SANDBOX_CONCURRENCY must be between 1 and 16")
        manager = SandboxManager(engine_client(), image=os.environ.get("SANDBOX_IMAGE", "agent-backend-sandbox:0.1.0"), concurrency=concurrency)

    async def janitor():
        while True:
            with suppress(SandboxError):
                await manager.reap()
            await asyncio.sleep(30)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        task = asyncio.create_task(janitor())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            if owns_client:
                await manager.client.aclose()

    async def authorize(request: Request):
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer ") or not secrets.compare_digest(header[7:].encode(), configured_token.encode()):
            raise SandboxError("BROKER_AUTH_REQUIRED", 401)

    app = FastAPI(title="Private coding sandbox broker", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan, dependencies=[Depends(authorize)])

    @app.exception_handler(SandboxError)
    async def sandbox_error(_request: Request, exc: SandboxError):
        return JSONResponse({"error": {"code": exc.code}}, status_code=exc.status)

    async def body(request: Request) -> dict[str, Any]:
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > MAX_PAYLOAD:
                raise SandboxError("REQUEST_TOO_LARGE", 413)
            data.extend(chunk)
        try:
            payload = json.loads(data)
        except (ValueError, UnicodeError, RecursionError):
            raise SandboxError("INVALID_JSON") from None
        if not isinstance(payload, dict):
            raise SandboxError("JSON_OBJECT_REQUIRED")
        return payload

    def tool_result(result: dict[str, Any]):
        # Helpers use a JSON error envelope. Never let an unsuccessful file write
        # or restore be mistaken for HTTP success by the public API.
        return JSONResponse(result, status_code=422) if "error" in result else result

    @app.get("/health")
    async def health():
        await manager.request("GET", "/_ping")
        return {"status": "ok", "execution_network": "none", "maximum_command_seconds": 300}

    @app.post("/workspaces", status_code=201)
    async def create_workspace(request: Request):
        payload = await body(request)
        identity = payload.pop("workspace_id", None)
        if set(payload) not in ({"files"}, {"archive_base64"}):
            raise SandboxError("PROVIDE_FILES_OR_ARCHIVE")
        return await manager.create(identity, payload)

    @app.post("/workspaces/{identity}/fork", status_code=201)
    async def fork_workspace(identity: str, request: Request):
        payload = await body(request)
        if set(payload) != {"workspace_id"}:
            raise SandboxError("TARGET_WORKSPACE_REQUIRED")
        return await manager.fork(identity, payload["workspace_id"])

    @app.post("/workspaces/{identity}/tools")
    async def tool(identity: str, request: Request):
        payload = await body(request)
        if set(payload) - {"name", "arguments"} or not isinstance(payload.get("name"), str) or payload["name"] not in TOOLS or not isinstance(payload.get("arguments", {}), dict):
            raise SandboxError("INVALID_TOOL")
        return tool_result(await manager.tool(identity, payload["name"], payload.get("arguments", {})))

    @app.post("/workspaces/{identity}/restore")
    async def restore(identity: str, request: Request):
        payload = await body(request)
        if set(payload) != {"archive_base64"}:
            raise SandboxError("ARCHIVE_REQUIRED")
        return tool_result(await manager.tool(identity, "restore", payload))

    @app.post("/workspaces/{identity}/verify")
    async def verify(identity: str, request: Request):
        return tool_result(await manager.verify(identity, await body(request)))

    @app.get("/workspaces/{identity}/archive")
    async def archive(identity: str):
        result = await manager.tool(identity, "archive", {})
        if "error" in result:
            return JSONResponse(result, status_code=422)
        try:
            content = base64.b64decode(result["archive_base64"], validate=True)
        except (ValueError, KeyError, TypeError):
            raise SandboxError("INVALID_SANDBOX_ARCHIVE", 502) from None
        return Response(content, media_type="application/zip", headers={"Content-Disposition": 'attachment; filename="workspace.zip"'})

    @app.get("/workspaces/{identity}/diff")
    async def diff(identity: str):
        return tool_result(await manager.tool(identity, "get_diff", {}))

    @app.delete("/workspaces/{identity}")
    async def delete(identity: str):
        return await manager.delete(identity)

    return app

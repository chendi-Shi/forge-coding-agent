import os

import httpx
from fastapi import HTTPException


async def broker(method: str, path: str, *, raw=False, **kwargs):
    token = os.environ.get("BROKER_TOKEN", "")
    if len(token) < 32:
        raise HTTPException(503, "Coding sandbox is not configured")
    try:
        async with httpx.AsyncClient(
            base_url=os.environ.get("BROKER_URL", "http://sandbox-broker:8090"),
            headers={"Authorization": "Bearer " + token}, timeout=300, trust_env=False,
        ) as client:
            response = await client.request(method, path, **kwargs)
    except httpx.HTTPError:
        raise HTTPException(503, "Sandbox unavailable; retry after checking its health") from None
    if response.is_error:
        # Only the internal broker's structured, bounded detail reaches clients.
        try:
            data = response.json()
            detail = str(data.get("detail") or data.get("error") or "Sandbox operation failed")[:500]
        except ValueError:
            detail = "Sandbox operation failed"
        raise HTTPException(response.status_code, detail)
    if raw:
        return response.content
    data = response.json()
    if isinstance(data, dict) and data.get("error"):
        raise HTTPException(422, str(data["error"])[:500])
    return data


async def tool(workspace_id: str, name: str, arguments: dict):
    return await broker("POST", f"/workspaces/{workspace_id}/tools", json={"name": name, "arguments": arguments})

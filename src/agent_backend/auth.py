"""Aegra auth handlers. Import must succeed even when configuration is absent."""

from langgraph_sdk import Auth

from agent_backend.settings import identity_from_header

auth = Auth()


@auth.authenticate
async def authenticate(headers: dict) -> dict:
    identity = identity_from_header(headers.get("authorization", ""))
    if identity is None:
        raise Auth.exceptions.HTTPException(status_code=401, detail="Invalid bearer token")
    return {"identity": identity, "display_name": identity, "is_authenticated": True}


@auth.on.threads
async def own_threads(ctx, value):
    # In-place mutation is persisted by Aegra on create; filters also cover
    # by-id reads, state, history, deletion and runs under the thread.
    if ctx.action == "create":
        value["metadata"] = {**(value.get("metadata") or {}), "owner": ctx.user.identity}
    if ctx.action == "create_run" and value.get("multitask_strategy") not in {None, "reject"}:
        raise Auth.exceptions.HTTPException(
            status_code=422, detail="This service supports only multitask_strategy=reject."
        )
    return {"metadata": {"owner": ctx.user.identity}}


@auth.on.assistants
async def shared_agent_definition(ctx, value):
    # Graphs are deployed as code. Clients may use the shared graph definition,
    # but may not replace it through the API.
    return ctx.action in {"read", "search"}


@auth.on
async def deny_unconfigured_resources(ctx, value):
    # Store namespaces and scheduled runs need explicit application policies.
    return False

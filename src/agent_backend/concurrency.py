"""Database-enforced, cross-replica single active run per conversation."""

from starlette.responses import JSONResponse

ACTIVE_RUN_INDEX = "uq_agent_backend_active_thread_run"
ACTIVE_RUN_DDL = f"""
CREATE UNIQUE INDEX IF NOT EXISTS {ACTIVE_RUN_INDEX}
ON runs (thread_id) WHERE status IN ('pending', 'running')
"""


async def integrity_error_response(_request, exception):
    # SQLAlchemy's asyncpg adapter retains the original PostgreSQL diagnostic
    # in __cause__. Match the named index, never arbitrary exception text.
    current = exception.orig
    for _ in range(4):
        if current is None:
            break
        if getattr(current, "constraint_name", None) == ACTIVE_RUN_INDEX:
            return JSONResponse(
                {"detail": "This thread already has a pending or running task."},
                status_code=409,
            )
        current = current.__cause__
    return JSONResponse({"detail": "Database constraint violation."}, status_code=500)

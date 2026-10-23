from types import SimpleNamespace

import pytest

from agent_backend.concurrency import ACTIVE_RUN_INDEX, integrity_error_response


@pytest.mark.asyncio
async def test_only_named_active_run_constraint_maps_to_conflict():
    original = Exception("database details must not reach clients")
    original.constraint_name = ACTIVE_RUN_INDEX
    adapter = Exception("adapter")
    adapter.__cause__ = original
    response = await integrity_error_response(None, SimpleNamespace(orig=adapter))
    assert response.status_code == 409
    assert b"database details" not in response.body

    original.constraint_name = "unrelated_unique_constraint"
    response = await integrity_error_response(None, SimpleNamespace(orig=adapter))
    assert response.status_code == 500
    assert b"database details" not in response.body

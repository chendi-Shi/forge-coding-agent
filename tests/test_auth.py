from types import SimpleNamespace

import pytest

from agent_backend.auth import auth, own_threads


@pytest.mark.parametrize("value", [{}, {"metadata": None}, {"metadata": {"owner": "attacker", "label": "ok"}}])
async def test_create_thread_metadata_accepts_null_and_forces_owner(value):
    ctx = SimpleNamespace(action="create", user=SimpleNamespace(identity="alice"))
    result = await own_threads(ctx, value)
    assert value["metadata"]["owner"] == "alice"
    assert result == {"metadata": {"owner": "alice"}}


@pytest.mark.parametrize("strategy", ["enqueue", "interrupt", "rollback"])
async def test_unsupported_thread_concurrency_strategy_is_explicit(strategy):
    ctx = SimpleNamespace(action="create_run", user=SimpleNamespace(identity="alice"))
    with pytest.raises(auth.exceptions.HTTPException) as error:
        await own_threads(ctx, {"multitask_strategy": strategy})
    assert error.value.status_code == 422

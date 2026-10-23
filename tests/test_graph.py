import asyncio

import pytest

from agent_backend.graph import build_graph
from agent_backend.settings import AgentSettings


async def test_mock_runs_overlap_and_do_not_mix_messages():
    graph = build_graph(AgentSettings(agent_mode="mock"))
    results = await asyncio.gather(*(
        graph.ainvoke({"messages": [("user", f"job-{i}")], "mock_delay_seconds": 0.2})
        for i in range(12)
    ))
    for index, result in enumerate(results):
        assert result["probe"]["request_id"] == f"job-{index}"
        assert result["messages"][-1].content == f"Mock completed: job-{index}"
    # There is an interval where all graph calls are in the async I/O node.
    assert max(r["probe"]["started_at"] for r in results) < min(r["probe"]["finished_at"] for r in results)


@pytest.mark.parametrize("delay", [-1, 61, float("nan"), float("inf")])
async def test_probe_delay_is_bounded(delay):
    graph = build_graph(AgentSettings(agent_mode="mock"))
    with pytest.raises(ValueError, match="finite"):
        await graph.ainvoke({"messages": [("user", "probe")], "mock_delay_seconds": delay})

import copy

import pytest

from agent_backend.forge.context import MemoryArtifactStore
from agent_backend.forge.providers import ScriptedProvider
from agent_backend.forge.runtime import AgentRuntime, Budget, RuntimeFailure

TASK = {"prompt": "Repair the incorrect result without modifying tests", "test_command": "pytest -q"}


def decision(name="", arguments=None):
    return {"action": "tool" if name else "finish", "tool": name, "arguments": arguments or {}, "summary": "repair"}


class Harness:
    def __init__(self, decisions, execute, budget=None):
        self.state, self.events = None, []
        self.runtime = AgentRuntime(provider=ScriptedProvider(decisions), execute=execute, checkpoint=self.save,
                                    emit=self.emit, artifacts=MemoryArtifactStore(), budget=budget)

    async def save(self, value):
        self.state = copy.deepcopy(value)

    async def emit(self, value):
        self.events.append(value)


async def test_false_finish_forces_test_feedback_and_repair():
    fixed, calls = False, []

    async def execute(call_id, name, args):
        nonlocal fixed
        calls.append(name)
        if name == "replace_text":
            fixed = True
            return {"replacements": 1}
        return {"exit_code": 0 if fixed else 1, "stdout": "assert 1 == 2"}

    harness = Harness([decision(), decision("replace_text", {"path": "a.py", "old_text": "1", "new_text": "2"}), decision()], execute)
    result = await harness.runtime.run(TASK)
    assert result["status"] == "completed"
    assert result["repairs"] == 1
    assert calls == ["run_tests", "replace_text", "run_tests"]
    assert any(event["type"] == "runtime.repair" for event in harness.events)


async def test_crash_after_effect_recovers_same_call_without_duplicate_mutation():
    operations, mutations = {}, []
    crashed = False

    async def execute(call_id, name, args):
        nonlocal crashed
        if call_id in operations:
            return operations[call_id]
        if name == "replace_text":
            mutations.append(args)
            operations[call_id] = {"replacements": 1}
            if not crashed:
                crashed = True
                raise ConnectionError("acknowledgement lost after committed tool result")
        else:
            operations[call_id] = {"exit_code": 0}
        return operations[call_id]

    first = Harness([decision("replace_text", {"path": "x", "old_text": "a", "new_text": "b"})], execute)
    with pytest.raises(ConnectionError):
        await first.runtime.run(TASK)
    assert first.state["pending"]["call_id"] == "step-1"
    resumed = Harness([decision()], execute)
    result = await resumed.runtime.run(TASK, first.state)
    assert len(mutations) == 1
    assert result["step"] == 2
    assert result["status"] == "completed"


async def test_unknown_tool_outcome_does_not_advance_or_choose_new_action():
    async def uncertain(*args):
        raise RuntimeFailure("UNCERTAIN_OPERATION")

    harness = Harness([decision("run_command", {"command": "touch marker"}), decision()], uncertain)
    with pytest.raises(RuntimeFailure, match="UNCERTAIN"):
        await harness.runtime.run(TASK)
    assert harness.state["step"] == 1
    assert harness.state["pending"]["name"] == "run_command"


async def test_step_budget_survives_resume():
    async def execute(*args):
        return {"files": ["a.py"]}

    first = Harness([decision("list_files")], execute, Budget(max_steps=1))
    with pytest.raises(RuntimeFailure, match="STEP_BUDGET"):
        await first.runtime.run(TASK)
    resumed = Harness([decision()], execute, Budget(max_steps=100))
    with pytest.raises(RuntimeFailure, match="STEP_BUDGET"):
        await resumed.runtime.run(TASK, first.state)
    assert resumed.state["step"] == 1


async def test_repeated_failures_exhaust_repair_budget():
    async def execute(*args):
        return {"exit_code": 1, "stdout": "still failing"}

    harness = Harness([decision(), decision()], execute, Budget(max_repairs=1))
    with pytest.raises(RuntimeFailure, match="REPAIR_BUDGET"):
        await harness.runtime.run(TASK)
    assert harness.state["repairs"] == 2
    assert harness.state["status"] == "running"


async def test_provider_receives_bounded_context_and_no_native_tool_calls():
    async def execute(call_id, name, args):
        return {"exit_code": 0} if name == "run_tests" else {"stdout": "long output " * 10000}

    harness = Harness([decision("run_command", {"command": "pytest -q"}), decision()], execute)
    await harness.runtime.run(TASK)
    windows = [event for event in harness.events if event["type"] == "runtime.context"]
    assert all(event["estimated_tokens"] <= 12000 for event in windows)
    assert len(str(harness.state["messages"])) < 20000


async def test_over_budget_model_response_is_saved_but_cannot_mutate():
    effects = []

    async def execute(*args):
        effects.append(args)
        return {}

    class CostlyProvider:
        async def decide(self, *args, **kwargs):
            return {"decision": decision("write_file", {"path": "x", "content": "changed"}),
                    "usage": {"prompt_tokens": 10, "completion_tokens": 2}}

    harness = Harness([], execute, Budget(max_total_tokens=5))
    harness.runtime.provider = CostlyProvider()
    with pytest.raises(RuntimeFailure, match="TOKEN_BUDGET"):
        await harness.runtime.run(TASK)
    assert not effects
    assert harness.state["usage"] == {"input_tokens": 10, "output_tokens": 2}
    assert harness.state["pending"] is None


async def test_exhausted_repair_budget_cannot_be_reset_by_resume():
    async def execute(*args):
        pytest.fail("an exhausted run must not execute another tool")

    harness = Harness([decision()], execute, Budget(max_repairs=1))
    state = harness.runtime.initial(TASK)
    state["repairs"] = 2
    with pytest.raises(RuntimeFailure, match="REPAIR_BUDGET"):
        await harness.runtime.run(TASK, state)


async def test_persisted_deadline_blocks_resumed_pending_effect():
    async def execute(*args):
        pytest.fail("expired task must not dispatch a pending side effect")

    harness = Harness([], execute)
    state = harness.runtime.initial(TASK)
    state["deadline"] = 1
    state["pending"] = {"call_id": "step-1", "name": "run_command", "arguments": {"command": "touch late"}}
    with pytest.raises(RuntimeFailure, match="TIME_BUDGET"):
        await harness.runtime.run(TASK, state)


async def test_working_notes_survive_compaction_and_recovery():
    note = "Confirmed bug is in the descriptor binding; next change the wrapper, do not repeat source discovery."

    async def execute(*args):
        return {"exit_code": 0}

    class InspectingProvider:
        async def decide(self, messages, *args, **kwargs):
            assert any(note in message["content"] for message in messages)
            return {"decision": decision(), "usage": {}}

    harness = Harness([], execute)
    state = harness.runtime.initial(TASK)
    state["memory"] = note
    state["messages"].extend({"role": "user", "content": "old observation " * 3000} for _ in range(10))
    harness.runtime.provider = InspectingProvider()
    result = await harness.runtime.run(TASK, state)
    assert result["status"] == "completed"
    assert any(event["type"] == "runtime.context" and event["dropped_messages"] > 0 for event in harness.events)

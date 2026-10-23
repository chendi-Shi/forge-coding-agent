import copy
import json

import pytest

from agent_backend.forge.context import (
    ContextBudgetExceeded,
    ContextBuilder,
    LoopDetector,
    MemoryArtifactStore,
    estimate_tokens,
    message_tokens,
    truncate_text,
)


def exchange(identity, content):
    return [
        {"role": "assistant", "content": None, "tool_calls": [{"id": identity, "type": "function", "function": {"name": "read_file", "arguments": json.dumps({"path": identity + ".py"})}}]},
        {"role": "tool", "tool_call_id": identity, "content": content},
    ]


def assert_complete_exchanges(messages):
    pending = set()
    for message in messages:
        if message.get("tool_calls"):
            assert not pending
            pending = {call["id"] for call in message["tool_calls"]}
        elif message["role"] == "tool":
            assert message["tool_call_id"] in pending
            pending.remove(message["tool_call_id"])
        else:
            assert not pending
    assert not pending


def test_large_history_is_bounded_without_orphaning_tool_results_or_mutating_inputs():
    store = MemoryArtifactStore()
    messages = [{"role": "system", "content": "Operate only on the selected repository."}]
    for number in range(30):
        messages.extend([{"role": "user", "content": f"Inspect source number {number}"}, *exchange(str(number), "line\n" * 2000)])
    messages.append({"role": "user", "content": "Fix the authorization bug and run the tests."})
    original = copy.deepcopy(messages)
    result = ContextBuilder(max_tokens=1500, reserve_tokens=200, observation_tokens=120, artifact_store=store).build(messages)
    assert result.estimated_tokens <= 1300
    assert result.estimated_tokens == sum(map(message_tokens, result.messages))
    assert result.messages[0] == original[0]
    assert result.messages[-1] == original[-1]
    assert result.dropped_messages > 0 and result.summary
    assert len(result.artifacts) == 30
    assert all(store.get(reference.uri) == "line\n" * 2000 for reference in result.artifacts)
    assert_complete_exchanges(result.messages)
    assert messages == original


def test_multi_call_assistant_and_all_its_results_are_an_atomic_group():
    first, second = exchange("one", "first"), exchange("two", "second")
    first[0]["tool_calls"].extend(second[0]["tool_calls"])
    messages = [first[0], second[1], first[1], {"role": "user", "content": "Continue"}]
    for budget in [128, 256, 512, 1024]:
        result = ContextBuilder(max_tokens=budget, observation_tokens=64).build(messages)
        assert result.estimated_tokens <= budget
        assert_complete_exchanges(result.messages)
        ids = {message.get("tool_call_id") for message in result.messages if message["role"] == "tool"}
        assert ids in (set(), {"one", "two"})


def test_recent_tool_traffic_cannot_displace_the_current_user_objective():
    objective = {"role": "user", "content": "Repair the authorization check, then verify the denied-owner case."}
    messages = [objective]
    for index in range(20):
        messages.extend(exchange(str(index), "long observation " * 200))
    result = ContextBuilder(max_tokens=500, observation_tokens=100).build(messages)
    assert objective in result.messages
    assert_complete_exchanges(result.messages)


def test_digest_itself_obeys_its_configured_cap_when_no_tool_group_fits():
    messages = []
    for index in range(10):
        pair = exchange(str(index), "observation")
        pair[0]["tool_calls"][0]["function"]["arguments"] = json.dumps({"content": "large tool argument" * 1000})
        messages.extend(pair)
    result = ContextBuilder(max_tokens=2000, summary_tokens=100).build(messages)
    assert message_tokens({"role": "user", "content": result.summary}) <= 100
    assert result.dropped_messages == len(messages)


def test_incomplete_and_orphan_observations_stay_in_artifacts_not_provider_messages():
    messages = [exchange("unfinished", "x")[0], {"role": "user", "content": "Resume"}, {"role": "tool", "tool_call_id": "lost", "content": "raw observation"}]
    store = MemoryArtifactStore()
    result = ContextBuilder(max_tokens=600, artifact_store=store).build(messages)
    assert_complete_exchanges(result.messages)
    assert not any(message.get("tool_calls") or message["role"] == "tool" for message in result.messages)
    assert store.get(result.artifacts[0].uri) == "raw observation"
    assert result.dropped_messages == 2


def test_observation_preview_keeps_head_and_failure_tail_and_full_original_is_retrievable():
    raw = "TEST START\n" + "passing\n" * 500 + "FINAL ERROR: permission denied"
    store = MemoryArtifactStore()
    result = ContextBuilder(max_tokens=1000, observation_tokens=100, artifact_store=store).build(exchange("call", raw))
    message = next(message for message in result.messages if message["role"] == "tool")
    assert "TEST START" in message["content"] and "permission denied" in message["content"]
    assert result.artifacts[0].uri in message["content"]
    assert store.get(result.artifacts[0].uri) == raw
    assert result.artifacts[0].bytes == len(raw.encode())


def test_newest_oversized_user_message_is_clipped_and_unicode_budget_is_explicit():
    result = ContextBuilder(max_tokens=256).build([{"role": "user", "content": "请检查权限" * 1000}])
    assert result.messages[-1]["role"] == "user"
    assert result.truncated_messages == 1 and result.estimated_tokens <= 256
    assert "heuristic" in result.token_estimator
    assert estimate_tokens("中文") == 2
    assert estimate_tokens("abc") == 1


def test_pinned_instructions_are_never_silently_truncated():
    with pytest.raises(ContextBudgetExceeded):
        ContextBuilder(max_tokens=128).build([{"role": "system", "content": "critical instruction " * 200}])


@pytest.mark.parametrize("budget", [0, 1, 2, 5, 20, 50])
def test_unicode_excerpt_never_exceeds_its_declared_estimate(budget):
    for tail in [False, True]:
        excerpt = truncate_text("头部中间尾部" * 100, budget, tail=tail)
        assert estimate_tokens(excerpt) <= budget


def test_repeated_calls_are_blocked_only_when_observation_is_also_unchanged():
    detector = LoopDetector(max_repeats=3)
    assert not detector.observe("read_file", {"path": "x.py"}, "old").blocked
    assert not detector.observe("read_file", {"path": "x.py"}, "old").blocked
    assert not detector.observe("read_file", {"path": "x.py"}, "changed").blocked
    assert not detector.observe("read_file", {"path": "x.py"}, "changed").blocked
    decision = detector.observe("read_file", {"path": "x.py"}, "changed")
    assert decision.blocked and decision.repetitions == 3
    detector.reset()
    assert not detector.observe("read_file", {"path": "x.py"}, "changed").blocked


def test_alternating_observation_cycle_is_detected_with_a_bounded_window():
    detector = LoopDetector(max_repeats=3, window_size=6)
    decisions = [detector.observe("poll", {}, value) for value in ["a", "b", "a", "b", "a", "b"]]
    assert not any(decision.blocked for decision in decisions[:-1])
    assert decisions[-1].blocked and decisions[-1].period == 2
    assert len(detector.history) == 6


def test_argument_key_order_does_not_bypass_repeat_detection():
    detector = LoopDetector(max_repeats=2)
    assert not detector.observe("search", {"query": "lease", "limit": 2}, {"hits": []}).blocked
    assert detector.observe("search", {"limit": 2, "query": "lease"}, {"hits": []}).blocked


def test_interleaved_distinct_successful_work_is_not_a_loop():
    detector = LoopDetector()
    calls = [
        ("read_file", {"path": "config.py"}, "same"),
        ("write_file", {"path": "first.py"}, "written"),
        ("read_file", {"path": "config.py"}, "same"),
        ("write_file", {"path": "second.py"}, "written"),
        ("read_file", {"path": "config.py"}, "same"),
    ]
    assert not any(detector.observe(*call).blocked for call in calls)


def test_an_artifact_reference_cannot_silently_exceed_observation_budget():
    class LongUriStore:
        def put(self, text, kind):
            return "artifact://" + "a" * 480

    with pytest.raises(ContextBudgetExceeded, match="reference alone"):
        ContextBuilder(observation_tokens=32, artifact_store=LongUriStore()).build(exchange("x", "output"))


def test_checkpointed_observations_keep_the_original_artifact_without_double_wrapping():
    store = MemoryArtifactStore()
    builder = ContextBuilder(max_tokens=1000, artifact_store=store)
    pair = exchange("persisted", "original raw observation " * 1000)
    pair[1], original_reference = builder.observation(pair[1])
    checkpoint = json.loads(json.dumps(pair))
    window = builder.build(checkpoint)
    assert len(store.items) == 1
    assert window.artifacts == [original_reference]
    assert window.messages[-1]["content"].count("Observation stored at") == 1
    assert "_artifact_reference" not in window.messages[-1]
    assert store.get(window.artifacts[0].uri) == "original raw observation " * 1000

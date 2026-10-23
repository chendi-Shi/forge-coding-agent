"""Pure checks for the fault-injection target boundary; never invoke Docker."""

from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest

from scripts import recovery_test
from scripts.recovery_test import PROJECT, ProbeError, canonical_uuid, select_owner


def container(name: str, *, project: str = PROJECT, health: str = "healthy") -> dict:
    return {
        "id": name, "hostname": name, "running": True, "health": health,
        "labels": {"com.docker.compose.project": project, "com.docker.compose.service": "api"},
    }


def test_owner_matches_complete_hostname_pid_and_worker_suffix():
    target, survivors = select_owner("worker-host-123-worker-0", [container("worker-host"), container("other-host")])
    assert target["id"] == "worker-host"
    assert [replica["id"] for replica in survivors] == ["other-host"]
    for owner in ("worker-host-other-123-worker-0", "worker-host", "worker-host-123", "worker-host-0-worker-1"):
        with pytest.raises(ProbeError, match="OWNER_NOT_UNIQUELY_IDENTIFIED"):
            select_owner(owner, [container("worker-host"), container("other-host")])


def test_owner_from_another_project_or_service_cannot_be_selected():
    replicas = [container("foreign", project="someone-else"), container("ours")]
    with pytest.raises(ProbeError, match="OWNER_NOT_UNIQUELY_IDENTIFIED"):
        select_owner("foreign-123-worker-0", replicas)
    replicas[0]["labels"]["com.docker.compose.project"] = PROJECT
    replicas[0]["labels"]["com.docker.compose.service"] = "postgres"
    with pytest.raises(ProbeError, match="OWNER_NOT_UNIQUELY_IDENTIFIED"):
        select_owner("foreign-123-worker-0", replicas)


def test_a_distinct_healthy_survivor_is_mandatory():
    with pytest.raises(ProbeError, match="NO_HEALTHY_SURVIVING_REPLICA"):
        select_owner("owner-123-worker-0", [container("owner"), container("other", health="starting")])
    with pytest.raises(ProbeError, match="OWNER_CONTAINER_NOT_HEALTHY"):
        select_owner("owner-123-worker-0", [container("owner", health="unhealthy"), container("other")])
    duplicate = deepcopy(container("owner"))
    duplicate["id"] = "another-id"
    with pytest.raises(ProbeError, match="OWNER_NOT_UNIQUELY_IDENTIFIED"):
        select_owner("owner-123-worker-0", [container("owner"), duplicate])


def test_database_query_id_must_be_canonical_uuid():
    expected = "2f7e0600-1f46-4a67-a6d4-225d80288525"
    assert canonical_uuid(expected) == expected
    for invalid in ("anything", "x' OR '1'='1", expected + "'; DROP TABLE runs;--", expected.replace("-", "")):
        with pytest.raises(ProbeError, match="INVALID_PROBE_UUID"):
            canonical_uuid(invalid)


def test_missing_injection_flag_cannot_create_runs_or_change_containers(monkeypatch):
    requests = []
    actual_client = httpx.Client

    def handler(request):
        requests.append((request.method, request.url.path))
        assert request.method == "GET" and request.url.path == "/v1/info"
        return httpx.Response(200, json={"mode": "mock"})

    class FakeProject:
        def replicas(self):
            return [dict(container("owner"), name="owner"), dict(container("other"), name="other")]

    monkeypatch.setattr(recovery_test, "DockerProject", FakeProject)
    monkeypatch.setattr(recovery_test, "command_output", lambda *_a, **_k: pytest.fail("No container commands permitted"))
    monkeypatch.setattr(recovery_test.httpx, "Client", lambda **kwargs: actual_client(transport=httpx.MockTransport(handler), **kwargs))
    report = recovery_test.execute(SimpleNamespace(base_url="http://127.0.0.1:2026", inject_crash=False), "test-only")
    assert not report["passed"] and not report["crash_injected"]
    assert report["errors"] == [{"stage": "preflight", "error": "INJECTION_FLAG_REQUIRED"}]
    assert requests == [("GET", "/v1/info")]


def test_failed_kill_command_still_restores_verified_container(monkeypatch):
    restored = []
    actual_client = httpx.Client
    thread_id = "2f7e0600-1f46-4a67-a6d4-225d80288525"
    run_id = "3f7e0600-1f46-4a67-a6d4-225d80288525"
    replicas = [dict(container("owner"), name="owner"), dict(container("other"), name="other")]

    def handler(request):
        if request.url.path == "/v1/info":
            return httpx.Response(200, json={"mode": "mock"})
        if request.url.path == "/threads":
            return httpx.Response(200, json={"thread_id": thread_id})
        if request.method == "POST":
            return httpx.Response(200, json={"run_id": run_id})
        return httpx.Response(200, json={"status": "running"})

    class FakeProject:
        docker = ["never-run-docker"]

        def replicas(self):
            return replicas

        def inspect(self, container_id):
            return next(replica for replica in replicas if replica["id"] == container_id)

        def owner(self, _run_id, _thread_id):
            return "owner-123-worker-0"

        def restore(self, selected, _timeout):
            restored.append(selected["id"])
            return selected

    def failed_kill(argv, **_kwargs):
        assert argv == ["never-run-docker", "kill", "--signal", "KILL", "owner"]
        raise ProbeError("SIMULATED_KILL_FAILURE")

    monkeypatch.setattr(recovery_test, "DockerProject", FakeProject)
    monkeypatch.setattr(recovery_test, "command_output", failed_kill)
    monkeypatch.setattr(recovery_test.httpx, "Client", lambda **kwargs: actual_client(transport=httpx.MockTransport(handler), **kwargs))
    report = recovery_test.execute(SimpleNamespace(base_url="http://127.0.0.1:2026", inject_crash=True, timeout=180, health_timeout=180), "test-only")
    assert not report["passed"]
    assert restored == ["owner"]
    assert report["restored_healthy"]
    assert report["errors"] == [{"stage": "inject_crash", "error": "SIMULATED_KILL_FAILURE"}]

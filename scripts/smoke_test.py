"""Exercise real HTTP auth, durable background runs, SSE and same-thread isolation."""

import asyncio
import json
import os
import time
from pathlib import Path

import httpx


def local_token():
    if os.environ.get("AUTH_TOKEN"):
        return os.environ["AUTH_TOKEN"]
    for line in (Path(__file__).resolve().parents[1] / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("API_KEYS_JSON="):
            return next(iter(json.loads(line.split("=", 1)[1]).values()))
    raise RuntimeError("Set AUTH_TOKEN or initialize .env")


async def main():
    base = os.environ.get("BASE_URL", "http://127.0.0.1:2026")
    async with httpx.AsyncClient(base_url=base, timeout=30, trust_env=False) as client:
        assert (await client.get("/health")).status_code == 200
        assert (await client.post("/threads", json={})).status_code == 401
        assert (await client.post("/threads", json={}, headers={"Authorization": "Bearer invalid"})).status_code == 401
        client.headers["Authorization"] = "Bearer " + local_token()
        info = await client.get("/v1/info")
        info.raise_for_status()
        assert info.json()["mode"] == "mock", "Smoke test requires mock mode"
        thread = await client.post("/threads", json={})
        thread.raise_for_status()
        tid = thread.json()["thread_id"]
        try:
            disabled = await client.post(f"/threads/{tid}/commands", json={"id": 1, "method": "run.start", "params": {}})
            assert disabled.status_code == 503 and "FF_V2_EVENT_STREAMING=false" in disabled.text
            unsupported = await client.post(f"/threads/{tid}/runs", json={
                "assistant_id": "agent", "input": {"messages": [{"role": "user", "content": "unsupported"}]},
                "multitask_strategy": "enqueue",
            })
            assert unsupported.status_code == 422
            first = await client.post(f"/threads/{tid}/runs", json={
                "assistant_id": "agent", "input": {"messages": [{"role": "user", "content": "smoke"}], "mock_delay_seconds": 3},
                "multitask_strategy": "reject", "durability": "sync",
            })
            first.raise_for_status()
            rid = first.json()["run_id"]
            duplicate = await client.post(f"/threads/{tid}/runs", json={
                "assistant_id": "agent", "input": {"messages": [{"role": "user", "content": "conflict"}]},
                "multitask_strategy": "reject",
            })
            assert duplicate.status_code == 409, f"Expected same-thread conflict; got {duplicate.status_code}"
            for endpoint in ("stream", "wait"):
                conflict = await client.post(f"/threads/{tid}/runs/{endpoint}", json={
                    "assistant_id": "agent", "input": {"messages": [{"role": "user", "content": "conflict"}]},
                    "multitask_strategy": "reject",
                })
                assert conflict.status_code == 409, f"Expected {endpoint} conflict; got {conflict.status_code}"
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                run = await client.get(f"/threads/{tid}/runs/{rid}")
                run.raise_for_status()
                status = run.json()["status"]
                if status == "success":
                    break
                assert status not in {"error", "timeout", "interrupted"}, f"Run failed: {status}"
                await asyncio.sleep(0.2)
            else:
                raise AssertionError("Run did not finish")
            state = await client.get(f"/threads/{tid}/state")
            state.raise_for_status()
            assert state.json()["values"]["probe"]["request_id"] == "smoke"
            async with client.stream("POST", f"/threads/{tid}/runs/stream", json={
                "assistant_id": "agent", "input": {"messages": [{"role": "user", "content": "stream-smoke"}], "mock_delay_seconds": 0.1},
                "stream_mode": ["values"], "on_disconnect": "continue",
            }) as response:
                response.raise_for_status()
                assert "text/event-stream" in response.headers.get("content-type", "")
                text = (await response.aread()).decode()
                assert "stream-smoke" in text and "event:" in text
            # Competing requests can reach different replicas. The DB must
            # admit exactly one, including while the accepted run is pending.
            race = await asyncio.gather(*[
                client.post(f"/threads/{tid}/runs", json={
                    "assistant_id": "agent",
                    "input": {"messages": [{"role": "user", "content": f"race-{index}"}], "mock_delay_seconds": 10},
                    "multitask_strategy": "reject",
                }) for index in range(8)
            ])
            codes = [response.status_code for response in race]
            assert codes.count(200) == 1 and codes.count(409) == 7, f"Non-atomic admission: {codes}"
        finally:
            await client.delete(f"/threads/{tid}")
        print("PASS: authentication, background execution, persistence, SSE, 8-way atomic same-thread rejection")


if __name__ == "__main__":
    asyncio.run(main())

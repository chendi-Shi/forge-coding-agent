"""Submit a prompt to the running backend and print the final answer."""

import argparse
import asyncio
import os
import sys

import httpx
from load_test import TERMINAL_STATUSES, read_auth_token


async def run(args):
    async with httpx.AsyncClient(
        base_url=args.base_url,
        headers={"Authorization": "Bearer " + read_auth_token()},
        timeout=30,
        trust_env=False,
    ) as client:
        tid = args.thread_id
        if not tid:
            response = await client.post("/threads", json={})
            response.raise_for_status()
            tid = response.json()["thread_id"]
        response = await client.post(f"/threads/{tid}/runs", json={
            "assistant_id": "agent",
            "input": {"messages": [{"role": "user", "content": args.prompt}]},
            "multitask_strategy": "reject",
            "durability": "sync",
        })
        response.raise_for_status()
        rid = response.json()["run_id"]
        print(f"thread_id={tid}\nrun_id={rid}", flush=True)
        async with asyncio.timeout(args.deadline):
            while True:
                response = await client.get(f"/threads/{tid}/runs/{rid}")
                response.raise_for_status()
                status = response.json()["status"]
                if status in TERMINAL_STATUSES:
                    if status != "success":
                        raise RuntimeError(f"Run ended with status {status}")
                    break
                await asyncio.sleep(0.5)
        response = await client.get(f"/threads/{tid}/state")
        response.raise_for_status()
        messages = response.json()["values"]["messages"]
        print(messages[-1]["content"])


if __name__ == "__main__":
    # Windows pipes may otherwise encode Chinese output with a legacy codepage.
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt")
    parser.add_argument("--thread-id", help="Continue an existing conversation")
    parser.add_argument("--base-url", default=os.environ.get("BASE_URL", "http://127.0.0.1:2026"))
    parser.add_argument("--deadline", type=float, default=600)
    asyncio.run(run(parser.parse_args()))

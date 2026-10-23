"""Trusted sandbox image entrypoint; requests are uploaded to a separate volume."""

import json
from pathlib import Path

from helper import SandboxError, dispatch
from runner import run_command


def main():
    try:
        request_file = Path("/input/request.json")
        if request_file.is_symlink() or request_file.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("Invalid sandbox input")
        payload = json.loads(request_file.read_bytes())
        request_file.unlink()
        if not isinstance(payload, dict) or not isinstance(payload.get("arguments", {}), dict):
            raise ValueError("Invalid tool request")
        result = run_command(payload["arguments"]) if payload.get("name") == "run_command" else dispatch(payload)
        print(json.dumps(result, ensure_ascii=True))
    except SandboxError as exc:
        print(json.dumps({"error": {"code": exc.code, "message": str(exc)[:500]}}))
    except ValueError as exc:
        print(json.dumps({"error": {"code": "INVALID_TOOL_REQUEST", "message": str(exc)[:500]}}))
    except Exception:
        print(json.dumps({"error": {"code": "SANDBOX_TOOL_FAILED", "message": "Sandbox operation failed"}}))


if __name__ == "__main__":
    main()

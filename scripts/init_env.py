"""Create local credentials once, without printing them or overwriting a file."""

import json
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    target = ROOT / ".env"
    if target.exists():
        content = target.read_text(encoding="utf-8")
        if not any(line.startswith("BROKER_TOKEN=") and line.split("=", 1)[1].strip() for line in content.splitlines()):
            content = "\n".join(line for line in content.splitlines() if not line.startswith("BROKER_TOKEN="))
            target.write_text(content + f"\nBROKER_TOKEN={secrets.token_urlsafe(40)}\n", encoding="utf-8", newline="\n")
            print("Added internal broker credential; existing credentials preserved.")
        else:
            print(".env already exists; credentials preserved.")
        return
    template = (ROOT / ".env.example").read_text(encoding="utf-8")
    keys = {"local-client": secrets.token_urlsafe(32), "isolation-test": secrets.token_urlsafe(32)}
    template = template.replace("POSTGRES_PASSWORD=\n", f"POSTGRES_PASSWORD={secrets.token_urlsafe(32)}\n")
    template = template.replace("API_KEYS_JSON={}", "API_KEYS_JSON=" + json.dumps(keys, separators=(",", ":")))
    template = template.replace("BROKER_TOKEN=\n", f"BROKER_TOKEN={secrets.token_urlsafe(40)}\n")
    with target.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(template)
    target.chmod(0o600)
    print("Created .env with random credentials. Tokens are not printed.")


if __name__ == "__main__":
    main()

"""Shared wire contract for UTF-8 observation artifacts (not arbitrary files)."""

import hashlib
import math
import re

MAX_ARTIFACT_BYTES = 1_048_576
MAX_TASK_ARTIFACT_BYTES = 16_777_216
MAX_TASK_ARTIFACTS = 256
PAGE_CHARACTERS = 8000
ARTIFACT_ID = re.compile(r"[a-f0-9]{64}")
ARTIFACT_KIND = re.compile(r"[a-z][a-z0-9_]{0,63}")


def describe_artifact(content: str, kind: str) -> dict:
    if not isinstance(kind, str) or not ARTIFACT_KIND.fullmatch(kind):
        raise ValueError("Invalid artifact kind")
    if not isinstance(content, str) or "\0" in content:
        raise ValueError("Artifact must be UTF-8 text without NUL characters")
    try:
        raw = content.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError("Artifact must be valid UTF-8 text") from error
    if len(raw) > MAX_ARTIFACT_BYTES:
        raise ValueError("Artifact exceeds 1 MiB")
    identity = hashlib.sha256(kind.encode() + b"\0" + raw).hexdigest()
    return {"uri": "artifact://" + identity, "kind": kind, "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw), "estimated_tokens": math.ceil(len(raw) / 3)}


def artifact_id(uri: str) -> str:
    if not isinstance(uri, str) or not uri.startswith("artifact://"):
        raise ValueError("Invalid artifact URI")
    identity = uri.removeprefix("artifact://")
    if not ARTIFACT_ID.fullmatch(identity):
        raise ValueError("Invalid artifact URI")
    return identity


def checkpoint_references(state: dict) -> dict[str, dict]:
    """Only the runtime's explicit metadata is a reference; quoted tool text is data."""
    references = {}
    messages = state.get("messages", [])
    if not isinstance(messages, list):
        raise ValueError("Checkpoint messages must be a list")
    for message in messages:
        if not isinstance(message, dict):
            raise ValueError("Checkpoint messages must be objects")
        reference = message.get("_artifact_reference")
        if reference is None:
            continue
        if not isinstance(reference, dict):
            raise ValueError("Invalid checkpoint artifact reference")
        identity = artifact_id(reference.get("uri"))
        if (message.get("role") != "tool" or not isinstance(message.get("content"), str)
                or not message["content"].startswith(f"[Observation stored at {reference['uri']}; bytes={reference.get('bytes')}]\n")):
            raise ValueError("Artifact reference does not match its observation")
        if identity in references and references[identity] != reference:
            raise ValueError("Conflicting checkpoint artifact references")
        references[identity] = reference
        if len(references) > MAX_TASK_ARTIFACTS:
            raise ValueError("Too many checkpoint artifact references")
    return references

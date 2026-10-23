"""Worker staging buffer backed by the authenticated server artifact API.

put() is synchronous to match ContextBuilder. flush() must finish before the
checkpoint that publishes its references; reads always use the server, so a
different worker needs no original filesystem. Local files are only an explicit
migration source for checkpoints created before durable artifacts existed.
"""

import asyncio
from pathlib import Path

import httpx

from agent_backend.forge.artifact_data import (
    MAX_ARTIFACT_BYTES,
    MAX_TASK_ARTIFACT_BYTES,
    MAX_TASK_ARTIFACTS,
    PAGE_CHARACTERS,
    artifact_id,
    checkpoint_references,
    describe_artifact,
)


class ArtifactPersistenceError(ValueError):
    pass


class ServerArtifacts:
    def __init__(self, client: httpx.AsyncClient, task_id: str, lease: str, *, legacy_directory: Path | None = None):
        self.client, self.task_id, self.lease = client, task_id, lease
        self.legacy_directory = legacy_directory
        self.pending: dict[str, tuple[str, dict]] = {}
        self.confirmed: dict[str, dict] = {}

    def put(self, text: str, kind: str) -> str:
        try:
            reference = describe_artifact(text, kind)
        except ValueError as error:
            raise ArtifactPersistenceError(str(error)) from error
        key = artifact_id(reference["uri"])
        if key not in self.confirmed and key not in self.pending:
            if (len(self.pending) + len(self.confirmed) >= MAX_TASK_ARTIFACTS
                    or sum(value[1]["bytes"] for value in self.pending.values()) + reference["bytes"] > MAX_TASK_ARTIFACT_BYTES):
                raise ArtifactPersistenceError("ARTIFACT_STAGING_QUOTA_EXCEEDED")
            self.pending[key] = (text, reference)
        return reference["uri"]

    async def request(self, method, key, **kwargs):
        response = await self.client.request(method, f"/api/coding/tasks/{self.task_id}/artifacts/{key}",
                                             headers={"X-Lease-Token": self.lease}, **kwargs)
        if response.status_code != 404 and response.is_error:
            raise ArtifactPersistenceError(f"ARTIFACT_HTTP_{response.status_code}")
        return response

    async def flush(self):
        for key, (content, reference) in list(self.pending.items()):
            # A PUT has immutable content: safely repeat after one lost response.
            for attempt in range(2):
                try:
                    response = await self.request("PUT", key, json={"kind": reference["kind"], "content": content})
                    break
                except httpx.TransportError:
                    if attempt:
                        raise
            if response.is_error:
                raise ArtifactPersistenceError(f"ARTIFACT_HTTP_{response.status_code}")
            received = response.json()
            if any(received.get(field) != value for field, value in reference.items()):
                raise ArtifactPersistenceError("ARTIFACT_INTEGRITY_MISMATCH")
            self.confirmed[key] = reference
            del self.pending[key]

    async def prepare_checkpoint(self, state: dict):
        """Flush new bytes and fail closed on a missing pre-migration artifact."""
        await self.flush()
        for key, reference in checkpoint_references(state).items():
            if key in self.confirmed:
                if self.confirmed[key] != reference:
                    raise ArtifactPersistenceError("ARTIFACT_INTEGRITY_MISMATCH")
                continue
            response = await self.request("GET", key, params={"limit": 1})
            if response.status_code == 404:
                path = self.legacy_directory / f"{key}.txt" if self.legacy_directory else None
                try:
                    if path is None:
                        raise FileNotFoundError
                    # The digest is strictly validated; never accept a path from
                    # checkpoint metadata. Bound legacy reads before allocating.
                    def read_legacy(path=path):
                        if path.is_symlink() or path.stat().st_size > MAX_ARTIFACT_BYTES:
                            raise ValueError("Invalid legacy artifact")
                        with path.open(encoding="utf-8") as stream:
                            return stream.read(MAX_ARTIFACT_BYTES + 1)
                    raw = await asyncio.to_thread(read_legacy)
                except (OSError, ValueError) as error:
                    raise ArtifactPersistenceError("LEGACY_ARTIFACT_UNAVAILABLE: original worker bytes are required") from error
                if describe_artifact(raw, reference.get("kind")) != reference:
                    raise ArtifactPersistenceError("LEGACY_ARTIFACT_INTEGRITY_MISMATCH")
                self.put(raw, reference["kind"])
                await self.flush()
            else:
                received = response.json()
                if any(received.get(field) != value for field, value in reference.items()):
                    raise ArtifactPersistenceError("ARTIFACT_INTEGRITY_MISMATCH")
                self.confirmed[key] = reference

    async def read(self, uri: str, offset: int = 0):
        try:
            key = artifact_id(uri)
        except ValueError:
            return {"error": "Invalid artifact URI"}
        if type(offset) is not int or offset < 0:
            return {"error": "Artifact offset must be a non-negative character index"}
        response = await self.request("GET", key, params={"offset": offset, "limit": PAGE_CHARACTERS})
        if response.status_code == 404:
            return {"error": "ARTIFACT_NOT_FOUND", "uri": uri}
        return response.json()

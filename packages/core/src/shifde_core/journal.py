"""Durable step receipts; uncertain native writes require explicit reconciliation."""

import json
from pathlib import Path
from typing import Protocol
from uuid import UUID

from filelock import FileLock

from shifde_core.artifact_store import atomic_write
from shifde_core.hashing import canonical_bytes, digest


class NeedsAttention(RuntimeError):
    pass


class Journal(Protocol):
    tenant_id: UUID
    operation_id: UUID

    def intent(self, key: str, inputs: dict) -> bool: ...
    def complete(self, key: str, receipt: dict) -> None: ...
    def completed(self, key: str) -> dict | None: ...
    def has_intent(self, key: str) -> bool: ...


class StepJournal:
    def __init__(self, path: Path, tenant_id: UUID, operation_id: UUID):
        self.path, self.tenant_id, self.operation_id = path, tenant_id, operation_id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = FileLock(str(path) + ".lock", timeout=5)
        with self._lock:
            if self.path.exists():
                self._load()

    def _load(self) -> dict:
        data = (
            json.loads(self.path.read_bytes())
            if self.path.exists()
            else {
                "tenant_id": str(self.tenant_id),
                "operation_id": str(self.operation_id),
                "steps": {},
            }
        )
        if data["tenant_id"] != str(self.tenant_id) or data["operation_id"] != str(
            self.operation_id
        ):
            raise PermissionError("journal belongs to another tenant or operation")
        return data

    def intent(self, key: str, inputs: dict) -> bool:
        with self._lock:
            data = self._load()
            previous = data["steps"].get(key)
            fingerprint = digest(inputs)
            if previous:
                if previous["input_digest"] != fingerprint:
                    raise ValueError("step inputs changed")
                if previous["state"] != "complete":
                    raise NeedsAttention(f"step {key} has an uncertain native outcome")
                return False
            data["steps"][key] = {"state": "intent", "input_digest": fingerprint}
            atomic_write(self.path, canonical_bytes(data))
            return True

    def complete(self, key: str, receipt: dict) -> None:
        with self._lock:
            data = self._load()
            entry = data["steps"][key]
            if entry["state"] == "complete" and entry["receipt"] != receipt:
                raise ValueError("conflicting step receipt")
            entry.update(state="complete", receipt=receipt)
            atomic_write(self.path, canonical_bytes(data))

    def completed(self, key: str) -> dict | None:
        with self._lock:
            entry = self._load()["steps"].get(key)
            return entry["receipt"] if entry and entry["state"] == "complete" else None

    def has_intent(self, key: str) -> bool:
        with self._lock:
            return key in self._load()["steps"]

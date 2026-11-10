"""Local durable outbox for native receipt metadata, never credentials or source."""

import json
import os
from pathlib import Path
from uuid import UUID, uuid4

from shifde_contracts.jobs import CheckpointSubmission
from shifde_core.hashing import digest


def sync_directory(path: Path):
    # Windows has no portable directory-fsync API; production hosts are Linux.
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class ReceiptOutbox:
    def __init__(self, settings):
        namespace = digest(
            [str(settings.tenant_id), str(settings.pool_id), str(settings.worker_id)]
        )[:32]
        self.root = settings.state_dir.resolve() / namespace
        existing_parent = self.root
        while not existing_parent.exists():
            existing_parent = existing_parent.parent
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        current = self.root
        while True:
            sync_directory(current)
            if current == existing_parent:
                break
            current = current.parent

    def remember(self, lease, data):
        request = CheckpointSubmission(lease_id=lease.lease_id, fence=lease.fence, **data)
        if request.phase != "complete":
            return None
        payload = {"job_id": str(lease.job.id), "request": request.model_dump(mode="json")}
        encoded = json.dumps(payload).encode()
        if len(encoded) > 20 * 1024:
            raise ValueError("receipt metadata exceeds its bound")
        destination = self.root / f"{lease.job.id.hex}-{request.sequence}.json"
        temporary = self.root / f"{uuid4().hex}.tmp"
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, destination)
            sync_directory(self.root)
        finally:
            temporary.unlink(missing_ok=True)
        return destination

    def acknowledge(self, path: Path | None):
        if path is not None:
            path.unlink(missing_ok=True)
            sync_directory(self.root)

    def has_pending(self, job_id):
        return any(self.root.glob(f"{job_id.hex}-*.json"))

    def replay(self, client):
        remaining = 0
        for path in self.root.glob("*.json"):
            try:
                if path.stat().st_size > 20 * 1024:
                    raise ValueError("invalid receipt file")
                payload = json.loads(path.read_bytes())
                identity = UUID(payload["job_id"])
                request = CheckpointSubmission.model_validate(payload["request"])
                client.replay_checkpoint(identity, request.model_dump(mode="json"))
                self.acknowledge(path)
            except Exception:
                # Expired/fenced receipts remain available to the operator; never force a replay.
                remaining += 1
        return remaining

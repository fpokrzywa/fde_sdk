from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from shifde_contracts.common import ResourceBinding, Target, utcnow

from shifde_core.artifact_store import ArtifactStore
from shifde_core.journal import Journal


@dataclass(frozen=True, kw_only=True)
class BuildContext:
    tenant_id: UUID
    artifacts: ArtifactStore
    toolchain_digest: str
    workspace: Path
    operation_id: UUID = field(default_factory=uuid4)


@dataclass(frozen=True, kw_only=True)
class ReadContext:
    tenant_id: UUID
    target: Target
    owned_resources: tuple[ResourceBinding, ...] = ()
    operation_id: UUID = field(default_factory=uuid4)
    deadline: datetime = field(default_factory=lambda: utcnow() + timedelta(hours=1))

    def __post_init__(self):
        if self.target.tenant_id != self.tenant_id:
            raise PermissionError("target belongs to another tenant")

    def check_deadline(self):
        if utcnow() >= self.deadline:
            raise TimeoutError("operation deadline exceeded")


@dataclass(frozen=True, kw_only=True)
class DeployContext(ReadContext):
    artifacts: ArtifactStore
    journal: Journal
    toolchain_digest: str
    write_guard: Callable[[], None] | None = None

    def check_deadline(self):
        super().check_deadline()
        if self.write_guard:
            self.write_guard()

    def __post_init__(self):
        super().__post_init__()
        if (
            self.journal.tenant_id != self.tenant_id
            or self.journal.operation_id != self.operation_id
        ):
            raise PermissionError("journal context mismatch")

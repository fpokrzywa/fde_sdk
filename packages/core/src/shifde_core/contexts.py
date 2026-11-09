from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from shifde_contracts.common import ResourceBinding, Target, utcnow

from shifde_core.artifact_store import LocalArtifactStore
from shifde_core.journal import StepJournal


@dataclass(frozen=True, kw_only=True)
class BuildContext:
    tenant_id: UUID
    artifacts: LocalArtifactStore
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
    artifacts: LocalArtifactStore
    journal: StepJournal
    toolchain_digest: str

    def __post_init__(self):
        super().__post_init__()
        if (
            self.journal.tenant_id != self.tenant_id
            or self.journal.operation_id != self.operation_id
        ):
            raise PermissionError("journal context mismatch")

"""Wire contracts for outbound execution; no secret values belong in these DTOs."""

from typing import Literal
from uuid import UUID, uuid4

from pydantic import Field

from shifde_contracts.common import (
    Digest,
    Entity,
    Identifier,
    Model,
    ResourceBinding,
    SourceArchive,
    Target,
    Timestamp,
)

ExecutionAction = Literal[
    "capabilities",
    "inspect",
    "scaffold",
    "validate",
    "build",
    "plan",
    "apply",
    "observe",
    "reconcile",
    "verify",
]
PinnedImage = str


class WorkerPool(Entity):
    name: Identifier
    mode: Literal["hosted", "private"]
    image: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._:/-]*@sha256:[a-f0-9]{64}$")
    actions: list[ExecutionAction] = Field(min_length=1)
    connection_ids: list[UUID] = Field(default_factory=list)
    enabled: bool = True


class ExecutionJob(Entity):
    operation_id: UUID
    execution_id: UUID
    operation_fence: int = Field(ge=1)
    pool_id: UUID
    action: ExecutionAction
    image: str
    request: dict
    target: Target | None = None
    owned_resources: list[ResourceBinding] = Field(default_factory=list)
    inputs: list[SourceArchive] = Field(default_factory=list)
    toolchain_digest: Digest
    deadline: Timestamp
    credential_ref: Identifier | None = None
    journal: dict[str, dict] = Field(default_factory=dict)


class LeaseProof(Model):
    lease_id: UUID
    fence: int = Field(ge=1)


class JobLease(LeaseProof):
    job: ExecutionJob
    expires_at: Timestamp
    request_id: UUID


class LeaseRequest(Model):
    image: str
    request_id: UUID = Field(default_factory=uuid4)


class CheckpointSubmission(LeaseProof):
    sequence: int = Field(ge=1)
    phase: Literal["intent", "complete"]
    step: Identifier
    data: dict


class JobResultSubmission(LeaseProof):
    status: Literal["succeeded", "failed"]
    value: dict | None = None
    error_code: Literal["EXECUTION_FAILED", "CREDENTIAL_UNAVAILABLE", "SANDBOX_FAILED"] | None = (
        None
    )


class CredentialGrant(Model):
    tenant_id: UUID
    job_id: UUID
    worker_id: UUID
    pool_id: UUID
    target_id: UUID
    credential_ref: Identifier
    audience: str
    expires_at: Timestamp

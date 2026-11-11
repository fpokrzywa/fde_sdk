from typing import Literal
from uuid import UUID

from pydantic import Field

from shifde_contracts.builds import BuildArtifact
from shifde_contracts.common import Digest, Entity, Model, ResourceBinding
from shifde_contracts.plans import DeploymentPlan
from shifde_contracts.projects import ApplicationSpec


class ApplyRequest(Model):
    artifact: BuildArtifact
    plan: DeploymentPlan


class ApplyReceipt(Entity):
    application_id: UUID
    target_id: UUID
    artifact_digest: Digest
    resources: list[ResourceBinding]
    operation_handles: dict[str, str] = Field(default_factory=dict)


class ObserveRequest(Model):
    receipt: ApplyReceipt
    spec: ApplicationSpec | None = None


class ReconcileRequest(Model):
    plan: DeploymentPlan


class VerifyRequest(Model):
    receipt: ApplyReceipt
    spec: ApplicationSpec | None = None


class NativeObservation(Model):
    state: Literal["running", "succeeded", "failed", "unknown"]
    resources: list[ResourceBinding] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)


class ReconciliationResult(Model):
    state: Literal["applied", "not_applied", "in_progress", "ambiguous"]
    evidence: str
    resources: list[ResourceBinding] = Field(default_factory=list)


class Deployment(Entity):
    application_id: UUID
    target_id: UUID
    artifact_digest: Digest
    resources: list[ResourceBinding]
    healthy: bool

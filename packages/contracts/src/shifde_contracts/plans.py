from typing import Literal
from uuid import UUID

from pydantic import Field, JsonValue

from shifde_contracts.builds import BuildArtifact
from shifde_contracts.common import Digest, Entity, Model, ResourceBinding, Timestamp, utcnow


class EnvironmentSnapshot(Model):
    resources: list[ResourceBinding] = Field(default_factory=list)
    fingerprint: Digest
    captured_at: Timestamp = Field(default_factory=utcnow)


class PlanStep(Model):
    key: str
    action: Literal["create", "update", "start", "delete", "unknown"]
    resource_key: str
    reversible: bool = False


class DeploymentPlan(Entity):
    application_id: UUID
    target_id: UUID
    connection_revision: int
    artifact_id: UUID
    artifact_digest: Digest
    spec_digest: Digest
    bindings_digest: Digest
    adapter_version: str
    toolchain_digest: Digest
    snapshot: EnvironmentSnapshot
    steps: list[PlanStep]
    details: dict[str, JsonValue] = Field(default_factory=dict)
    created_at: Timestamp = Field(default_factory=utcnow)
    expires_at: Timestamp
    digest: Digest


class PlanRequest(Model):
    artifact: BuildArtifact


class InspectRequest(Model):
    application_id: UUID
    app_name: str

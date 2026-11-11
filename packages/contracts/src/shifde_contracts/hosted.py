from typing import Literal
from uuid import UUID

from pydantic import Field, JsonValue, field_validator

from shifde_contracts.common import (
    Digest,
    Entity,
    Identifier,
    Model,
    SourceArchive,
    Target,
    Timestamp,
)
from shifde_contracts.execution import Deployment
from shifde_contracts.operations import Operation
from shifde_contracts.projects import ApplicationSpec

ApplicationKind = Literal[
    "databricks.app/v1",
    "palantir.superrepo/v1",
    "snowflake.native-app/v1",
    "fabric.solution/v1",
    "fabric.iq/v1",
]


class ApplicationSubmission(Model):
    name: str = Field(min_length=1, max_length=100)
    kind: ApplicationKind


class ConnectionSubmission(Model):
    name: str = Field(min_length=1, max_length=100)
    host: str
    credential_ref: Identifier
    _host = field_validator("host")(Target._validate_host)


class Connection(Entity):
    name: str
    host: str
    credential_ref: str
    revision: int = 1


class TargetSubmission(Model):
    connection_id: UUID
    environment: Identifier = "dev"
    bindings: dict[str, str] = Field(default_factory=dict, max_length=50)


class TargetPatch(Model):
    environment: Identifier | None = None
    bindings: dict[str, str] | None = Field(default=None, max_length=50)


class ProjectSubmission(Model):
    application_id: UUID
    source_id: UUID
    spec: ApplicationSpec


class SourceUploadSubmission(Model):
    digest: Digest
    size: int = Field(ge=1, le=20 * 1024 * 1024)


class SourceUpload(Entity):
    digest: Digest
    size: int
    expires_at: Timestamp
    source: SourceArchive | None = None


class SourceUploadCompletion(Model):
    upload_id: UUID


class ScaffoldSubmission(Model):
    application_id: UUID
    spec: ApplicationSpec
    template_version: str = "0.1.0"


class InspectionSubmission(Model):
    application_id: UUID
    target_id: UUID


class ValidationSubmission(Model):
    project_id: UUID


class OperationControl(Model):
    operation_id: UUID


class PolicyDecisionSubmission(Model):
    operation_id: UUID
    plan_digest: Digest
    approve: bool


class OperationEvent(Model):
    seq: int
    name: str
    data: dict[str, JsonValue]
    created_at: Timestamp


class EventPage(Model):
    data: list[OperationEvent]
    next_cursor: int


class DeploymentView(Model):
    deployment: Deployment
    operation: Operation

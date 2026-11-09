from uuid import UUID

from shifde_contracts.common import Digest, Entity, Model, SourceArchive
from shifde_contracts.projects import ApplicationSpec


class BuildArtifact(Entity):
    application_id: UUID
    archive: SourceArchive
    source_digest: Digest
    spec_digest: Digest
    spec: ApplicationSpec
    adapter_version: str
    toolchain_digest: Digest
    template_version: str = "0.1.0"


class ScaffoldRequest(Model):
    spec: ApplicationSpec
    template_version: str = "0.1.0"


class ValidateRequest(Model):
    spec: ApplicationSpec
    source: SourceArchive


class BuildRequest(ValidateRequest):
    application_id: UUID

from typing import Annotated, Any, cast
from uuid import UUID

from pydantic import Field, TypeAdapter

from shifde_contracts.common import Entity, SourceArchive
from shifde_contracts.platform_specs.databricks import DatabricksApplicationSpec
from shifde_contracts.platform_specs.fabric import FabricApplicationSpec
from shifde_contracts.platform_specs.palantir import FoundryApplicationSpec
from shifde_contracts.platform_specs.snowflake import SnowflakeApplicationSpec

ApplicationSpec = Annotated[
    DatabricksApplicationSpec
    | FoundryApplicationSpec
    | SnowflakeApplicationSpec
    | FabricApplicationSpec,
    Field(discriminator="kind"),
]


def parse_spec(value: object) -> ApplicationSpec:
    # Pydantic accepts Annotated unions at runtime; its constructor annotation is narrower.
    return TypeAdapter[ApplicationSpec](cast(Any, ApplicationSpec)).validate_python(value)


class Application(Entity):
    name: str
    kind: str


class Project(Entity):
    application_id: UUID
    revision: int = Field(default=1, ge=1)
    spec: ApplicationSpec
    source: SourceArchive

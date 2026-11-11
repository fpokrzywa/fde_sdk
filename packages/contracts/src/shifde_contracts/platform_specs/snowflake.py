from typing import Literal

from pydantic import Field

from shifde_contracts.common import Identifier, Model, RelativePath


class SnowflakeApplicationSpec(Model):
    kind: Literal["snowflake.native-app/v1"] = "snowflake.native-app/v1"
    project_root: RelativePath = "."
    package_binding: Identifier
    application_binding: Identifier
    warehouse_binding: Identifier
    release_version: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,29}$")
    release_patch: int = Field(default=0, ge=0, le=129)

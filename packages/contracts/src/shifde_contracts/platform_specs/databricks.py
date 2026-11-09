from typing import Literal

from pydantic import Field

from shifde_contracts.common import Identifier, Model, RelativePath


class DatabricksApplicationSpec(Model):
    kind: Literal["databricks.app/v1"] = "databricks.app/v1"
    bundle_target: Identifier = "dev"
    bundle_root: RelativePath = "."
    app_resource_key: str = Field(default="app", pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
    warehouse_binding: Identifier = "warehouse"
    health_path: Literal["/health"] = "/health"

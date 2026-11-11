from typing import Literal
from uuid import UUID

from pydantic import Field

from shifde_contracts.common import Digest, Identifier, Model, RelativePath


class FoundryApplicationSpec(Model):
    kind: Literal["palantir.superrepo/v1"] = "palantir.superrepo/v1"
    superrepo_root: RelativePath = "."
    ontology_binding: Identifier
    osdk_package: str
    frontend_path: RelativePath = "frontend"
    product_binding: Identifier


class FoundryMarketplaceSpec(Model):
    kind: Literal["palantir.marketplace/v1"] = "palantir.marketplace/v1"
    version_id: UUID
    object_type: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.]{0,127}$")
    frontend_asset_path: RelativePath
    frontend_asset_sha256: Digest
    marketplace_binding: Identifier = "marketplace"
    product_binding: Identifier = "product"
    parent_folder_binding: Identifier = "parent_folder"
    namespace_binding: Identifier = "namespace"
    ontology_binding: Identifier = "ontology"
    frontend_origin_binding: Identifier = "frontend_origin"

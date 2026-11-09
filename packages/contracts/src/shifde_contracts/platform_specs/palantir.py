from typing import Literal

from shifde_contracts.common import Identifier, Model, RelativePath


class FoundryApplicationSpec(Model):
    kind: Literal["palantir.superrepo/v1"] = "palantir.superrepo/v1"
    superrepo_root: RelativePath = "."
    ontology_binding: Identifier
    osdk_package: str
    frontend_path: RelativePath = "frontend"
    product_binding: Identifier

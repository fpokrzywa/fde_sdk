import json
import re
from uuid import UUID

from shifde_contracts.common import Target
from shifde_contracts.platform_specs.palantir import FoundryMarketplaceSpec
from shifde_core.hashing import digest
from shifde_core.native import PALANTIR_PROFILE

ADAPTER_VERSION = "0.1.0"
TOOLCHAIN_DIGEST = digest(PALANTIR_PROFILE)
TERMINAL = "palantir-finalize"


def rid(value, kind):
    if not re.fullmatch(
        r"ri\.[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\." + re.escape(kind) + r"\.[a-fA-F0-9-]{36}", value
    ):
        raise ValueError("invalid Foundry resource RID")
    UUID(value.rsplit(".", 1)[1])
    return value


def bindings(spec, target):
    if not isinstance(spec, FoundryMarketplaceSpec):
        raise ValueError("Marketplace delivery requires palantir.marketplace/v1")
    b = target.bindings
    return {
        "marketplace": rid(b[spec.marketplace_binding], "local"),
        "product": str(UUID(b[spec.product_binding])),
        "parent_folder": rid(b[spec.parent_folder_binding], "folder"),
        "namespace": rid(b[spec.namespace_binding], "namespace"),
        "ontology": rid(b[spec.ontology_binding], "ontology"),
        "frontend_origin": Target._validate_host(b[spec.frontend_origin_binding]),
    }


def files_for(spec):
    if not isinstance(spec, FoundryMarketplaceSpec):
        raise ValueError("SuperRepo requires enrollment-specific preparation")
    if spec.frontend_asset_path == ".":
        raise ValueError("frontend asset path must identify a file")
    return {
        "deployment.json": (
            json.dumps(spec.model_dump(mode="json"), sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
    }


def validate_source(files, spec):
    if files != files_for(spec):
        raise ValueError("Marketplace source must be the immutable approved-product descriptor")


def native_id(host, installation):
    return "palantir:" + digest(host) + ":" + rid(installation, "block-set-installation")

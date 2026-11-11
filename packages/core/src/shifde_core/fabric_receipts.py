"""Fabric receipt shape and accepted-checkpoint correlation, without native I/O."""

import re
from uuid import UUID

from shifde_core.hashing import digest
from shifde_core.native import fabric_step


def validate_fabric_receipt(receipt, spec, target, *, completed=None):
    if spec is None or spec.kind not in {"fabric.solution/v1", "fabric.iq/v1"}:
        raise ValueError("Fabric verification requires the immutable application spec")
    workspace = str(UUID(target.bindings["workspace"]))
    handles = receipt.operation_handles
    if (
        receipt.tenant_id != target.tenant_id
        or receipt.target_id != target.id
        or handles.get("kind") != spec.kind
        or handles.get("workspace") != workspace
        or handles.get("verification") != (spec.verification_notebook or "")
    ):
        raise ValueError("Fabric receipt/spec/target mismatch")
    resources = {r.key: r for r in receipt.resources}
    if len(resources) != len(receipt.resources) or set(resources) != {
        item.key for item in spec.items
    }:
        raise ValueError("Fabric receipt resource set is incomplete")
    allowed = {"workspace", "kind", "verification"}
    for item in spec.items:
        binding = resources[item.key]
        prefix = "fabric:" + workspace + ":"
        if (
            binding.application_id != receipt.application_id
            or binding.target_id != target.id
            or binding.resource_type != "fabric." + item.item_type
            or not binding.native_id.startswith(prefix)
        ):
            raise ValueError("Fabric receipt resource context mismatch")
        UUID(binding.native_id.removeprefix(prefix))
        name_key, description_key = item.key + ":name", item.key + ":description"
        allowed.update({name_key, description_key})
        if (
            handles.get(name_key) != target.bindings[item.name_binding or item.key]
            or description_key not in handles
            or len(handles[description_key]) > 256
        ):
            raise ValueError("Fabric receipt metadata evidence is missing")
        if item.item_type != "Lakehouse":
            key = item.key + ":definition"
            allowed.add(key)
            if not re.fullmatch(r"[a-f0-9]{64}", handles.get(key, "")):
                raise ValueError("Fabric definition evidence is missing")
        if completed:
            metadata = completed(fabric_step(item.key, "metadata-complete"))
            if (
                not metadata
                or binding.model_dump(mode="json") not in metadata["resources"]
                or metadata["handles"].get("metadata_digest")
                != digest(
                    {"displayName": handles[name_key], "description": handles[description_key]}
                )
            ):
                raise ValueError("Fabric receipt differs from accepted metadata checkpoint")
            if item.item_type != "Lakehouse":
                definition = completed(fabric_step(item.key, "definition-complete")) or metadata
                if (
                    definition["handles"].get("definition_digest")
                    != handles[item.key + ":definition"]
                ):
                    raise ValueError("Fabric receipt differs from accepted definition checkpoint")
    if spec.verification_notebook:
        allowed.update({"job_id", "verification_item"})
        job_id = str(UUID(handles.get("job_id", "")))
        item_id = resources[spec.verification_notebook].native_id.removeprefix(
            "fabric:" + workspace + ":"
        )
        if handles.get("verification_item") != item_id:
            raise ValueError("Fabric verification item evidence differs from spec")
        if completed:
            proof = completed("fabric-verification-submit")
            if not proof or proof["handles"].get("job_id") != job_id:
                raise ValueError("Fabric job evidence differs from accepted checkpoint")
    if set(handles) != allowed:
        raise ValueError("Fabric receipt has missing or unexpected evidence")

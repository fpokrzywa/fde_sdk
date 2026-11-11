"""Server/worker proof checks for the narrow approved Marketplace delivery profile."""

import re
from uuid import UUID

from shifde_contracts.checkpoints import NativeStepReceipt

from shifde_core.hashing import digest


def job_payload(identity, spec):
    return {
        "installations": [
            {
                "installationRid": identity,
                "targetVersion": {"type": "versionId", "versionId": str(spec.version_id)},
                "inputs": {"values": {}, "explicitlyUnset": []},
            }
        ],
        "recommendations": [],
        "submissionMode": {"type": "submit", "errorTolerance": "NONE", "errorHandling": "ABORT"},
    }


def installation_rid(value):
    if not re.fullmatch(
        r"ri\.marketplace\.[A-Za-z0-9_-]+\.block-set-installation\.[a-fA-F0-9-]{36}", value
    ):
        raise ValueError("invalid Marketplace installation RID")
    UUID(value.rsplit(".", 1)[1])
    return value


def validate_palantir_step(step, target):
    allowed = (
        {"installation"}
        if step.step_key == "palantir-installation-create"
        else {"installation", "job_id", "request_digest"}
    )
    if (
        step.step_key not in {"palantir-installation-create", "palantir-job-submit"}
        or set(step.handles) != allowed
        or not target
    ):
        raise ValueError("unexpected Marketplace checkpoint handles")
    for key, value in step.handles.items():
        if key == "installation":
            installation_rid(value)
        elif key == "job_id":
            if not re.fullmatch(
                r"ri\.marketplace\.[A-Za-z0-9_-]+\.installation-job\.[a-fA-F0-9-]{36}", value
            ):
                raise ValueError("invalid installation job RID")
            UUID(value.rsplit(".", 1)[1])
        elif not re.fullmatch(r"[a-f0-9]{64}", value):
            raise ValueError("invalid native request digest")
    if len(step.resources) != 1:
        raise ValueError("Marketplace checkpoint requires exactly one installation")
    binding = step.resources[0]
    prefix = "palantir:" + digest(target.host) + ":"
    if (
        binding.key != "installation"
        or binding.resource_type != "palantir.marketplace-installation"
        or not binding.native_id.startswith(prefix)
    ):
        raise ValueError("Marketplace resource binding mismatch")
    identity = installation_rid(binding.native_id.removeprefix(prefix))
    if step.handles.get("installation") != identity:
        raise ValueError("Marketplace installation handle mismatch")


def validate_palantir_receipt(receipt, spec, target, *, completed=None, plan=None):
    if spec.kind != "palantir.marketplace/v1" or not target:
        raise ValueError("Marketplace receipt profile mismatch")
    handles = receipt.operation_handles
    if set(handles) != {"installation", "job_id", "request_digest", "schema_digest", "spec_digest"}:
        raise ValueError("Marketplace receipt handle mismatch")
    if handles["spec_digest"] != digest(spec) or not re.fullmatch(
        r"[a-f0-9]{64}", handles["schema_digest"]
    ):
        raise ValueError("Marketplace receipt specification mismatch")
    step = NativeStepReceipt(
        step_key="palantir-job-submit",
        plan_digest="0" * 64,
        resources=receipt.resources,
        handles={k: handles[k] for k in ("installation", "job_id", "request_digest")},
    )
    validate_palantir_step(step, target)
    if handles["request_digest"] != digest(job_payload(handles["installation"], spec)):
        raise ValueError("Marketplace receipt native request mismatch")
    if plan and handles["schema_digest"] != plan.details.get("schema_digest"):
        raise ValueError("Marketplace receipt schema differs from plan")
    if any(
        r.application_id != receipt.application_id or r.target_id != target.id
        for r in receipt.resources
    ):
        raise ValueError("Marketplace receipt ownership mismatch")
    if completed:
        for key in ("palantir-installation-create", "palantir-job-submit"):
            raw = completed(key)
            if not raw:
                raise ValueError("Marketplace native checkpoint missing")
            saved = NativeStepReceipt.model_validate(raw)
            validate_palantir_step(saved, target)
            if (
                saved.resources != receipt.resources
                or saved.handles["installation"] != handles["installation"]
            ):
                raise ValueError("Marketplace checkpoint identity mismatch")
            if key == "palantir-job-submit" and saved.handles != step.handles:
                raise ValueError("Marketplace job checkpoint mismatch")

from datetime import datetime

from shifde_contracts.common import Target, utcnow
from shifde_contracts.plans import DeploymentPlan

from shifde_core.hashing import digest


def bindings_digest(target: Target) -> str:
    return digest(target)


def seal_plan(plan: DeploymentPlan) -> DeploymentPlan:
    payload = plan.model_dump(mode="json", exclude={"digest"})
    return plan.model_copy(update={"digest": digest(payload)})


def validate_plan(
    plan: DeploymentPlan,
    target: Target,
    *,
    now: datetime | None = None,
    check_start_ttl: bool = True,
) -> None:
    if seal_plan(plan).digest != plan.digest:
        raise ValueError("plan digest mismatch")
    if plan.tenant_id != target.tenant_id:
        raise PermissionError("plan belongs to another tenant")
    if plan.target_id != target.id or plan.connection_revision != target.connection_revision:
        raise ValueError("target binding changed")
    if plan.bindings_digest != bindings_digest(target):
        raise ValueError("environment binding changed")
    if check_start_ttl and (now or utcnow()) >= plan.expires_at:
        raise ValueError("deployment plan expired")

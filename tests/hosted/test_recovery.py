from dataclasses import replace

import pytest
import sqlalchemy as sa
from shifde_contracts.common import utcnow
from shifde_contracts.hosted import PolicyDecisionSubmission, TargetPatch
from shifde_contracts.submissions import DeploymentSubmission
from shifde_hosted.errors import ServiceError
from shifde_hosted.executor import OperationRef
from shifde_hosted.persistence import models as m

pytestmark = pytest.mark.integration


def deploy(setup, key="deploy"):
    service, actor, _, _, _, artifact, plan = setup
    return service.submit(
        actor, "deployment", DeploymentSubmission(artifact_id=artifact.id, plan_id=plan.id), key
    )


def test_revoked_membership_prevents_queued_native_work(execution_setup):
    service, actor, executor, cli, *_ = execution_setup
    op = deploy(execution_setup)
    with service.database.transaction(actor.tenant_id) as c:
        c.execute(
            m.memberships.update()
            .where(m.memberships.c.subject == actor.subject)
            .values(active=False)
        )
    assert executor.execute(OperationRef(str(actor.tenant_id), str(op.id))) == "terminal"
    with service.database.transaction(actor.tenant_id) as c:
        assert (
            c.scalar(sa.select(m.operations.c.state).where(m.operations.c.id == op.id)) == "failed"
        )
    assert not cli.actions


def test_cancellation_after_acceptance_preserves_native_receipt(execution_setup):
    service, actor, executor, cli, *_ = execution_setup
    op = deploy(execution_setup)
    ref = OperationRef(str(actor.tenant_id), str(op.id))
    assert executor.execute(ref) == "submitted"
    service.cancel(actor, op.id)
    assert executor.observe(ref) == "terminal"
    result = service.get_operation(actor, op.id)
    assert result.state == "cancelled"
    assert result.result.value.resources and not result.result.value.healthy
    assert len(cli.actions) == 2


def test_receipt_recovery_redelegates_without_repeating_native_writes(execution_setup):
    service, actor, executor, cli, *_ = execution_setup
    op = deploy(execution_setup)
    ref = OperationRef(str(actor.tenant_id), str(op.id))
    assert executor.execute(ref) == "submitted"
    executor._failure(ref, "LOST_WORKER", attention=True)
    recovery_actor = replace(actor, subject="recovery-owner")
    with service.database.transaction(actor.tenant_id) as c:
        c.execute(
            m.memberships.insert().values(
                tenant_id=actor.tenant_id, subject=recovery_actor.subject, role="owner"
            )
        )
        c.execute(
            m.memberships.update()
            .where(m.memberships.c.subject == actor.subject)
            .values(active=False)
        )
        c.execute(m.operations.update().where(m.operations.c.id == op.id).values(deadline=utcnow()))
    service.resume(recovery_actor, op.id)
    assert executor.execute(ref) == "submitted"
    executor.observe(ref)
    assert service.get_operation(recovery_actor, op.id).state == "succeeded"
    assert len(cli.actions) == 2


def test_policy_decision_cannot_be_replaced_before_worker_consumes_it(execution_setup):
    service, actor, executor, cli, _, _, plan = execution_setup
    service.require_approval_environments = ("dev",)
    op = deploy(execution_setup)
    approver = replace(actor, subject="approver")
    with service.database.transaction(actor.tenant_id) as c:
        c.execute(
            m.memberships.insert().values(
                tenant_id=actor.tenant_id, subject=approver.subject, role="owner"
            )
        )
    decision = PolicyDecisionSubmission(operation_id=op.id, plan_digest=plan.digest, approve=True)
    with pytest.raises(ServiceError, match="INVALID_POLICY_DECISION"):
        service.decide_policy(actor, decision)
    service.decide_policy(approver, decision)
    with pytest.raises(ServiceError, match="POLICY_ALREADY_DECIDED"):
        service.decide_policy(approver, decision.model_copy(update={"approve": False}))


def test_target_is_reloaded_after_lock_before_native_writes(execution_setup, monkeypatch):
    service, actor, executor, cli, *_ = execution_setup
    op = deploy(execution_setup)
    original = service.target
    changed = False

    def concurrent_patch(connection, tenant, identity):
        nonlocal changed
        target = original(connection, tenant, identity)
        if not changed:
            changed = True
            service.patch_target(
                actor, identity, TargetPatch(bindings={"warehouse": "456"}), expected_revision=1
            )
        return target

    monkeypatch.setattr(service, "target", concurrent_patch)
    executor.execute(OperationRef(str(actor.tenant_id), str(op.id)))
    assert service.get_operation(actor, op.id).state == "failed"
    assert not cli.actions


def test_operator_connection_policy_cannot_be_bypassed_with_dev_label(execution_setup):
    service, actor, executor, cli, *_ = execution_setup
    service.require_approval_environments = ("prod",)
    service.credential_bindings["development"] = service.credential_bindings[
        "development"
    ].model_copy(update={"require_approval": True})
    op = deploy(execution_setup)
    assert op.state == "waiting_for_policy"
    assert executor.execute(OperationRef(str(actor.tenant_id), str(op.id))) == "waiting"
    assert not cli.actions


def test_execution_worker_rechecks_operator_approval_policy(execution_setup):
    service, actor, executor, cli, *_ = execution_setup
    op = deploy(execution_setup)
    service.credential_bindings["development"] = service.credential_bindings[
        "development"
    ].model_copy(update={"require_approval": True})
    assert executor.execute(OperationRef(str(actor.tenant_id), str(op.id))) == "waiting"
    assert service.get_operation(actor, op.id).state == "waiting_for_policy"
    assert not cli.actions

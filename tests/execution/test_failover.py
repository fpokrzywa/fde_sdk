import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest
import sqlalchemy as sa
from shifde_contracts.jobs import (
    CheckpointSubmission,
    JobResultSubmission,
    LeaseProof,
    LeaseRequest,
)
from shifde_contracts.submissions import DeploymentSubmission
from shifde_contracts.workflow import OperationRef
from shifde_hosted.errors import ServiceError
from shifde_hosted.persistence import models as m
from shifde_hosted.remote_adapter import GatewayAdapter

pytestmark = pytest.mark.integration


async def pending(gateway_setup):
    gateway, worker, pool, setup = gateway_setup
    service, actor, executor, _, _, artifact, plan = setup
    executor.adapter_factory = lambda _: GatewayAdapter(gateway, poll_seconds=0.01)
    op = service.submit(
        actor,
        "deployment",
        DeploymentSubmission(artifact_id=artifact.id, plan_id=plan.id),
        "leased",
    )
    task = asyncio.create_task(
        asyncio.to_thread(executor.execute, OperationRef(str(actor.tenant_id), str(op.id)))
    )
    async with asyncio.timeout(10):
        while True:
            lease = await asyncio.to_thread(gateway.lease, worker, LeaseRequest(image=pool.image))
            if lease:
                return lease, task, op
            await asyncio.sleep(0.01)


async def test_expired_lease_cannot_renew_checkpoint_or_result_and_never_reassigns(gateway_setup):
    gateway, worker, pool, setup = gateway_setup
    service, actor, _, cli, _, _, plan = setup
    lease, task, op = await pending(gateway_setup)
    proof = LeaseProof(lease_id=lease.lease_id, fence=lease.fence)
    with service.database.transaction(actor.tenant_id) as c:
        c.execute(
            m.execution_jobs.update()
            .where(m.execution_jobs.c.id == lease.job.id)
            .values(lease_until=sa.func.now() - sa.text("interval '1 second'"))
        )
    for call in (
        lambda: gateway.heartbeat(worker, lease.job.id, proof),
        lambda: gateway.credential_grant(worker, lease.job.id, proof),
        lambda: gateway.result(
            worker, lease.job.id, JobResultSubmission(**proof.model_dump(), status="failed")
        ),
    ):
        with pytest.raises(ServiceError):
            call()
    assert gateway.lease(worker, LeaseRequest(image=pool.image)) is None
    assert await asyncio.wait_for(task, 10) == "terminal"
    assert service.get_operation(actor, op.id).state == "needs_attention"
    assert not cli.actions
    with service.database.transaction(actor.tenant_id) as c:
        assert (
            c.scalar(
                sa.select(sa.func.count())
                .select_from(m.application_claims)
                .where(m.application_claims.c.operation_id == op.id)
            )
            == 1
        )


async def test_lost_checkpoint_ack_replays_once_and_conflicting_or_unknown_steps_fail(
    gateway_setup,
):
    gateway, worker, pool, setup = gateway_setup
    service, actor, *_ = setup
    lease, task, _ = await pending(gateway_setup)
    proof = dict(lease_id=lease.lease_id, fence=lease.fence)
    request = CheckpointSubmission(
        **proof,
        sequence=1,
        phase="intent",
        step="bundle-deploy",
        data={"plan": lease.job.request["plan"]["digest"]},
    )
    try:
        first = gateway.checkpoint(worker, lease.job.id, request)
        assert gateway.checkpoint(worker, lease.job.id, request) == first
        for invalid in (
            request.model_copy(update={"data": {"plan": "b" * 64}}),
            request.model_copy(update={"sequence": 2, "step": "shell"}),
            request.model_copy(update={"sequence": 3}),
        ):
            with pytest.raises(ServiceError):
                gateway.checkpoint(worker, lease.job.id, invalid)
        with pytest.raises(ServiceError):
            gateway.guard(
                worker, lease.job.id, LeaseProof(lease_id=lease.lease_id, fence=lease.fence + 1)
            )
        with pytest.raises(ServiceError):
            gateway.credential_grant(
                replace(worker, worker_id=uuid4()), lease.job.id, LeaseProof(**proof)
            )
        with service.database.transaction(actor.tenant_id) as c:
            assert (
                c.scalar(
                    sa.select(sa.func.count())
                    .select_from(m.execution_checkpoints)
                    .where(m.execution_checkpoints.c.job_id == lease.job.id)
                )
                == 1
            )
    finally:
        gateway.result(worker, lease.job.id, JobResultSubmission(**proof, status="failed"))
        await asyncio.wait_for(task, 10)


async def test_worker_poll_retry_returns_same_lease(gateway_setup):
    gateway, worker, pool, _ = gateway_setup
    lease, task, _ = await pending(gateway_setup)
    try:
        assert gateway.lease(worker, LeaseRequest(image=pool.image)) is None
        replay = gateway.lease(worker, LeaseRequest(image=pool.image, request_id=lease.request_id))
        assert (
            replay is not None
            and replay.lease_id == lease.lease_id
            and replay.job.id == lease.job.id
        )
    finally:
        gateway.result(
            worker,
            lease.job.id,
            JobResultSubmission(lease_id=lease.lease_id, fence=lease.fence, status="failed"),
        )
        await asyncio.wait_for(task, 10)


async def test_already_lost_job_returns_terminal_outcome(gateway_setup):
    gateway, worker, pool, setup = gateway_setup
    service, actor, executor, *_ = setup
    lease, task, op = await pending(gateway_setup)
    try:
        with service.database.transaction(actor.tenant_id) as c:
            c.execute(
                m.execution_jobs.update()
                .where(m.execution_jobs.c.id == lease.job.id)
                .values(state="lost")
            )
        assert gateway.outcome(lease.job) == {"status": "lost"}
    finally:
        executor._failure(
            OperationRef(str(actor.tenant_id), str(op.id)), "TEST_CLEANUP", attention=True
        )
        await asyncio.wait_for(task, 10)


async def test_revoked_pool_connection_cannot_obtain_native_grant(gateway_setup):
    gateway, worker, pool, setup = gateway_setup
    service, actor, executor, *_ = setup
    lease, task, op = await pending(gateway_setup)
    try:
        with service.database.transaction(actor.tenant_id) as c:
            c.execute(
                m.worker_pools.update()
                .where(m.worker_pools.c.id == pool.id)
                .values(data=pool.model_copy(update={"connection_ids": []}).model_dump(mode="json"))
            )
        with pytest.raises(ServiceError):
            gateway.credential_grant(
                worker, lease.job.id, LeaseProof(lease_id=lease.lease_id, fence=lease.fence)
            )
    finally:
        executor._failure(
            OperationRef(str(actor.tenant_id), str(op.id)), "TEST_CLEANUP", attention=True
        )
        await asyncio.wait_for(task, 10)

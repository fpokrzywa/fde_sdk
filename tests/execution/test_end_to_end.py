import asyncio
import os
from uuid import uuid4

import pytest
from temporalio.worker import Worker

from tests.execution.test_private_worker import outbound_setup as outbound_setup
from tests.execution.test_private_worker import (
    test_source_build_has_no_native_credential_grant as source_build_flow,
)
from tests.hosted.test_temporal import temporal_client as temporal_client

pytestmark = pytest.mark.integration


async def test_temporal_to_outbound_gateway_to_native_receipt_end_to_end(
    outbound_setup, temporal_client
):
    from shifde_control_worker.activities import ControlActivities, ExecutionActivities
    from shifde_control_worker.dispatcher import Dispatcher
    from shifde_control_worker.workflows import DeliveryWorkflow

    server, headers, supervisor, client, setup = outbound_setup
    gateway, worker, pool, execution = setup
    service, actor, executor, cli, _, artifact, plan = execution
    response = server.post(
        "/v1/deployments",
        json={"artifact_id": str(artifact.id), "plan_id": str(plan.id)},
        headers={**headers, "Idempotency-Key": "temporal-private-e2e"},
    )
    control_queue, execution_queue = "control-" + uuid4().hex, "execution-" + uuid4().hex
    control, native = ControlActivities(executor), ExecutionActivities(executor)
    dispatcher = Dispatcher(
        service.database,
        temporal_client,
        control_queue=control_queue,
        execution_queue=execution_queue,
        poll_interval=0.02,
        tenant_ids=[actor.tenant_id],
    )
    async with (
        Worker(
            temporal_client,
            task_queue=control_queue,
            workflows=[DeliveryWorkflow],
            activities=[control.record_failure],
        ),
        Worker(
            temporal_client, task_queue=execution_queue, activities=[native.execute, native.observe]
        ),
    ):
        await dispatcher.dispatch_once()
        complete = asyncio.create_task(
            temporal_client.get_workflow_handle(response.json()["id"]).result()
        )
        async with asyncio.timeout(30):
            while not complete.done():
                await asyncio.to_thread(supervisor.run_once)
                await asyncio.sleep(0.02)
            await complete
    assert server.get(response.headers["Location"], headers=headers).json()["state"] == "succeeded"
    assert len(cli.actions) == 2


@pytest.mark.container
async def test_http_gateway_supervisor_and_real_container_build_end_to_end(outbound_setup):
    from shifde_executor.sandbox import DockerSandbox
    from shifde_hosted.persistence import models as m

    image = os.getenv("SHIFDE_TEST_EXECUTOR_IMAGE")
    if not image:
        pytest.skip("requires SHIFDE_TEST_EXECUTOR_IMAGE")
    _, _, supervisor, client, setup = outbound_setup
    gateway, worker, pool, execution = setup
    service, actor, *_ = execution
    updated = pool.model_copy(update={"image": image, "mode": "hosted"})
    with service.database.transaction(actor.tenant_id) as c:
        c.execute(
            m.worker_pools.update()
            .where(m.worker_pools.c.id == pool.id)
            .values(data=updated.model_dump(mode="json"))
        )
    settings = supervisor.settings.model_copy(update={"image": image})
    supervisor.settings, client.settings = settings, settings
    supervisor.sandbox = DockerSandbox(settings)
    await source_build_flow(outbound_setup)

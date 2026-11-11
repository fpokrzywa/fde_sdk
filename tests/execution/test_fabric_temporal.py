import asyncio
from uuid import uuid4

import pytest
from temporalio.worker import Worker

from tests.execution.test_fabric_hosted import fabric_flow
from tests.execution.test_fabric_hosted import fabric_hosted as fabric_hosted
from tests.execution.test_private_worker import outbound_setup as outbound_setup
from tests.hosted.test_temporal import temporal_client as temporal_client

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("fabric_hosted", ["fabric.iq/v1"], indirect=True)
async def test_fabric_iq_through_temporal_and_private_workers(fabric_hosted, temporal_client):
    from shifde_control_worker.activities import ControlActivities, ExecutionActivities
    from shifde_control_worker.dispatcher import Dispatcher
    from shifde_control_worker.workflows import DeliveryWorkflow

    _, _, supervisor, _, _, _, service, actor, executor, *_ = fabric_hosted
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

    async def execute(ref):
        await dispatcher.dispatch_once()
        complete = asyncio.create_task(
            temporal_client.get_workflow_handle(ref.operation_id).result()
        )
        async with asyncio.timeout(120):
            while not complete.done():
                await asyncio.to_thread(supervisor.run_once)
                await asyncio.sleep(0.02)
            await complete
        history = await temporal_client.get_workflow_handle(ref.operation_id).fetch_history()
        assert "fabric-only-local-secret" not in str(history)

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
        await fabric_flow(fabric_hosted, execute=execute)

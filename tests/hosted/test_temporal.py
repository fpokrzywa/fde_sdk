import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from temporalio.client import Client
from temporalio.worker import Worker

pytestmark = pytest.mark.integration


@pytest.fixture
async def temporal_client():
    address = os.getenv("SHIFDE_TEST_TEMPORAL_ADDRESS")
    config = Path(".tools/hosted-dev.json")
    if not address and config.exists():
        address = json.loads(config.read_bytes())["temporal_address"]
    if not address:
        pytest.skip("Temporal integration requires SHIFDE_TEST_TEMPORAL_ADDRESS")
    return await Client.connect(address)


async def test_real_temporal_workflow_and_duplicate_outbox_do_not_repeat_work(
    execution_setup, temporal_client
):
    from shifde_control_worker.activities import ControlActivities, ExecutionActivities
    from shifde_control_worker.dispatcher import Dispatcher
    from shifde_control_worker.workflows import DeliveryWorkflow
    from shifde_hosted.persistence import repository as repository

    service, actor, executor, cli, workspace, artifact, plan = execution_setup
    from shifde_contracts.submissions import DeploymentSubmission

    op = service.submit(
        actor,
        "deployment",
        DeploymentSubmission(artifact_id=artifact.id, plan_id=plan.id),
        "temporal-deploy",
    )
    control, execution = "control-" + uuid4().hex, "execution-" + uuid4().hex
    native = ExecutionActivities(executor)
    coordinator = ControlActivities(executor)
    dispatcher = Dispatcher(
        service.database,
        temporal_client,
        control_queue=control,
        execution_queue=execution,
        poll_interval=0.05,
        tenant_ids=[actor.tenant_id],
    )
    async with (
        Worker(
            temporal_client,
            task_queue=control,
            workflows=[DeliveryWorkflow],
            activities=[coordinator.record_failure],
        ),
        Worker(temporal_client, task_queue=execution, activities=[native.execute, native.observe]),
    ):
        await dispatcher.dispatch_once()
        await temporal_client.get_workflow_handle(str(op.id)).result()
        assert service.get_operation(actor, op.id).state == "succeeded"
        assert len(cli.actions) == 2
        with service.database.transaction(actor.tenant_id) as connection:
            repository.enqueue(connection, actor.tenant_id, op.id)
        await dispatcher.dispatch_once()
        assert len(cli.actions) == 2
        history = await temporal_client.get_workflow_handle(str(op.id)).fetch_history()
        from google.protobuf.json_format import MessageToJson

        text = "".join(MessageToJson(event) for event in history.events)
        assert "statement_execution" not in text and "SHIFDE_TEST_NATIVE_TOKEN" not in text


def test_outbox_acknowledgement_is_fenced_by_lease(execution_setup):
    from shifde_hosted.persistence import models
    from shifde_hosted.persistence.outbox import OutboxRepository

    service, actor, *_ = execution_setup
    repo = OutboxRepository(service.database)
    first = repo.lease(actor.tenant_id, limit=1)[0]
    with service.database.transaction(actor.tenant_id) as connection:
        connection.execute(
            models.outbox.update()
            .where(models.outbox.c.id == first.id)
            .values(lease_until=sa.func.now() - sa.text("interval '1 minute'"))
        )
    replacement = repo.lease(actor.tenant_id, limit=1)[0]
    assert not repo.sent(first)
    assert repo.sent(replacement)

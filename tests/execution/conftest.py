import pytest

from tests.hosted.conftest import database as database
from tests.hosted.conftest import hosted_database_url as hosted_database_url
from tests.hosted.conftest import new_tenant as new_tenant
from tests.hosted.test_executor import execution_setup as execution_setup
from tests.hosted.test_service import hosted_service as hosted_service


@pytest.fixture
def gateway_setup(execution_setup):
    from uuid import uuid4

    from shifde_contracts.jobs import WorkerPool
    from shifde_hosted.gateway import ExecutionGateway, WorkerPrincipal
    from shifde_hosted.persistence import models as m

    service, actor, executor, cli, workspace, artifact, plan = execution_setup
    pool = WorkerPool(
        tenant_id=actor.tenant_id,
        name="private",
        mode="private",
        image="example/worker@sha256:" + "a" * 64,
        actions=[
            "apply",
            "observe",
            "verify",
            "build",
            "plan",
            "scaffold",
            "validate",
            "capabilities",
            "inspect",
        ],
        connection_ids=[service.get_resource(actor, "target", plan.target_id)[0].connection_id],
    )
    worker = WorkerPrincipal(actor.tenant_id, pool.id, uuid4(), "worker")
    with service.database.transaction(actor.tenant_id) as c:
        c.execute(
            m.worker_pools.insert().values(
                tenant_id=actor.tenant_id, id=pool.id, data=pool.model_dump(mode="json")
            )
        )
        c.execute(
            m.executors.insert().values(
                tenant_id=actor.tenant_id,
                id=worker.worker_id,
                pool_id=pool.id,
                subject=worker.subject,
            )
        )
    return ExecutionGateway(service), worker, pool, execution_setup

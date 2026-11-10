import asyncio
from uuid import UUID

from shifde_contracts.workflow import OperationRef
from shifde_hosted.persistence import repository as r
from shifde_hosted.persistence.database import Database
from shifde_hosted.persistence.outbox import OutboxRepository
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from shifde_control_worker.workflows import DeliveryWorkflow


class Dispatcher:
    def __init__(
        self,
        database: Database,
        client: Client,
        *,
        control_queue="shifde-control",
        execution_queue="shifde-execution",
        poll_interval: float = 2,
        tenant_ids: list[UUID] | None = None,
    ):
        self.database, self.client = database, client
        self.repo = OutboxRepository(database)
        self.control_queue, self.execution_queue, self.poll_interval = (
            control_queue,
            execution_queue,
            poll_interval,
        )
        self.tenant_ids = tenant_ids

    def _state(self, lease):
        with self.database.transaction(lease.tenant_id) as c:
            return r.operation(c, lease.tenant_id, lease.operation_id)["state"]

    async def dispatch_once(self):
        count = 0
        tenants = (
            self.tenant_ids
            if self.tenant_ids is not None
            else await asyncio.to_thread(self.repo.tenants)
        )
        for tenant in tenants:
            for lease in await asyncio.to_thread(self.repo.lease, tenant):
                try:
                    state = await asyncio.to_thread(self._state, lease)
                    if state not in r.TERMINAL:
                        if lease.kind == "wake":
                            await self.client.get_workflow_handle(str(lease.operation_id)).signal(
                                "wake"
                            )
                        else:
                            resume = lease.kind.startswith("resume:")
                            workflow_id = str(lease.operation_id) + (
                                f"/{lease.kind}" if resume else ""
                            )
                            await self.client.start_workflow(
                                DeliveryWorkflow.run,
                                OperationRef(
                                    str(tenant),
                                    str(lease.operation_id),
                                    resume,
                                    self.execution_queue,
                                    self.poll_interval,
                                ),
                                id=workflow_id,
                                task_queue=self.control_queue,
                                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                            )
                    await asyncio.to_thread(self.repo.sent, lease)
                    count += 1
                except WorkflowAlreadyStartedError:
                    await asyncio.to_thread(self.repo.sent, lease)
                except Exception:
                    await asyncio.to_thread(self.repo.failed, lease)
        return count

    async def run(self):
        while True:
            await self.dispatch_once()
            await asyncio.sleep(1)

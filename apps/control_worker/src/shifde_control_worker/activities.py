import asyncio
from uuid import UUID

from shifde_contracts.workflow import OperationRef
from shifde_hosted.executor import OperationExecutor
from shifde_hosted.persistence import repository as r
from temporalio import activity


class ExecutionActivities:
    def __init__(self, executor: OperationExecutor):
        self.executor = executor

    async def _run(self, function, ref):
        task = asyncio.create_task(asyncio.to_thread(function, ref))
        while not task.done():
            activity.heartbeat()
            await asyncio.wait({task}, timeout=5)
        return await task

    @activity.defn(name="shifde.execute")
    async def execute(self, ref: OperationRef) -> str:
        return await self._run(self.executor.execute, ref)

    @activity.defn(name="shifde.observe")
    async def observe(self, ref: OperationRef) -> str:
        return await self._run(self.executor.observe, ref)


class ControlActivities:
    def __init__(self, executor: OperationExecutor):
        self.executor = executor

    @activity.defn(name="shifde.record_failure")
    async def record_failure(self, ref: OperationRef) -> str:
        def record():
            with self.executor.database.transaction(UUID(ref.tenant_id)) as c:
                row = r.operation(c, UUID(ref.tenant_id), UUID(ref.operation_id))
                uncertain = row["kind"] == "deployment" and row["execution_id"] is not None
            return self.executor._failure(ref, "WORKER_INTERRUPTED", attention=uncertain)

        return await asyncio.to_thread(record)

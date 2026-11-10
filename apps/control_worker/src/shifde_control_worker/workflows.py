from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from shifde_contracts.workflow import OperationRef


@workflow.defn
class DeliveryWorkflow:
    def __init__(self):
        self.woken = False

    @workflow.signal
    def wake(self):
        self.woken = True

    @workflow.run
    async def run(self, ref: OperationRef) -> None:
        phase = "new"
        try:
            while phase != "terminal":
                self.woken = False
                name = "shifde.execute" if phase in ("new", "waiting") else "shifde.observe"
                phase = await workflow.execute_activity(
                    name,
                    ref,
                    result_type=str,
                    task_queue=ref.execution_queue,
                    start_to_close_timeout=timedelta(hours=1),
                    heartbeat_timeout=timedelta(seconds=30),
                    retry_policy=RetryPolicy(maximum_attempts=1),
                )
                if phase != "terminal":
                    try:
                        await workflow.wait_condition(
                            lambda: self.woken, timeout=timedelta(seconds=ref.poll_interval)
                        )
                    except TimeoutError:
                        pass
        except ActivityError:
            await workflow.execute_activity(
                "shifde.record_failure",
                ref,
                result_type=str,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=10),
            )

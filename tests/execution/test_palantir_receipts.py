import copy
from contextlib import contextmanager

import pytest
import sqlalchemy as sa
from shifde_contracts.jobs import CheckpointSubmission
from shifde_hosted.errors import ServiceError

from tests.execution.test_palantir_hosted import palantir_flow
from tests.execution.test_palantir_hosted import palantir_hosted as palantir_hosted
from tests.execution.test_private_worker import outbound_setup as outbound_setup

pytestmark = pytest.mark.integration


async def test_gateway_rejects_incomplete_or_uncorrelated_terminal_receipts(palantir_hosted):
    _, _, supervisor, gateway, worker, _, _, _, _, _, _, _ = palantir_hosted
    original_call, original_database = supervisor.client.call, gateway.database
    outcomes = []

    class RollbackDatabase:
        @contextmanager
        def transaction(self, tenant_id):
            with original_database.engine.connect() as c:
                transaction = c.begin()
                c.execute(
                    sa.text("SELECT set_config('shifde.tenant_id', :tenant, true)"),
                    {"tenant": str(tenant_id)},
                )
                try:
                    yield c
                finally:
                    transaction.rollback()

    def probe(lease, method, data=None):
        if (
            method == "checkpoints"
            and data["phase"] == "complete"
            and data["step"] == "palantir-finalize"
        ):
            for corruption in ["resources", "type", "job", "schema", "request", "spec"]:
                bad = copy.deepcopy(data)
                receipt = bad["data"]
                if corruption == "resources":
                    receipt["resources"] = []
                elif corruption == "type":
                    receipt["resources"][0]["resource_type"] = "palantir.other"
                elif corruption == "job":
                    receipt["operation_handles"]["job_id"] = (
                        "ri.marketplace.main.installation-job.11111111-1111-4111-8111-111111111111"
                    )
                else:
                    receipt["operation_handles"][corruption + "_digest"] = "f" * 64
                gateway.database = RollbackDatabase()
                try:
                    gateway.checkpoint(
                        worker,
                        lease.job.id,
                        CheckpointSubmission(lease_id=lease.lease_id, fence=lease.fence, **bad),
                    )
                except ServiceError:
                    outcomes.append(True)
                else:
                    outcomes.append(False)
                finally:
                    gateway.database = original_database
        return original_call(lease, method, data)

    supervisor.client.call = probe
    await palantir_flow(palantir_hosted)
    assert len(outcomes) == 12 and all(outcomes)

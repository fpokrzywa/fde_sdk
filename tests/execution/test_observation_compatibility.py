import pytest
import sqlalchemy as sa
from shifde_hosted.persistence import models as m

from tests.execution.test_private_worker import outbound_setup as outbound_setup
from tests.execution.test_private_worker import (
    test_outbound_private_executor_completes_native_delivery_through_http as delivery,
)

pytestmark = pytest.mark.integration


async def test_non_fabric_observation_jobs_preserve_existing_request_shape(outbound_setup):
    await delivery(outbound_setup)
    service, actor, *_ = outbound_setup[-1][-1]
    with service.database.transaction(actor.tenant_id) as c:
        jobs = c.execute(sa.select(m.execution_jobs.c.data)).scalars().all()
    observed = [job for job in jobs if job["action"] in {"observe", "verify"}]
    assert observed and all(set(job["request"]) == {"receipt"} for job in observed)

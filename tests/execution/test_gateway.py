from dataclasses import replace
from uuid import uuid4

import pytest
import sqlalchemy as sa
from shifde_contracts.jobs import LeaseRequest
from shifde_hosted.errors import ServiceError

pytestmark = pytest.mark.integration


def test_worker_identity_and_image_are_checked_before_leasing(gateway_setup):
    gateway, worker, pool, _ = gateway_setup
    with pytest.raises(ServiceError):
        gateway.lease(replace(worker, tenant_id=uuid4()), LeaseRequest(image=pool.image))
    with pytest.raises(ServiceError):
        gateway.lease(worker, LeaseRequest(image="example/untrusted:latest"))
    assert gateway.lease(worker, LeaseRequest(image=pool.image)) is None


def test_gateway_tables_are_rls_protected(gateway_setup):
    from shifde_hosted.persistence import models as m

    gateway, _, _, setup = gateway_setup
    service, actor, *_ = setup
    with service.database.transaction(uuid4()) as c:
        assert not c.execute(sa.select(m.worker_pools)).all()
        assert not c.execute(sa.select(m.executors)).all()

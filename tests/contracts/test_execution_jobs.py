from uuid import uuid4

import pytest
from pydantic import ValidationError


def test_worker_pool_requires_digest_pinned_image_and_explicit_actions():
    from shifde_contracts.jobs import WorkerPool

    values = dict(tenant_id=uuid4(), name="private", mode="private", actions=["build"])
    with pytest.raises(ValidationError):
        WorkerPool(**values, image="example/worker:latest")
    pool = WorkerPool(**values, image="example/worker@sha256:" + "a" * 64)
    assert pool.model_dump()["image"].endswith("a" * 64)
    with pytest.raises(ValidationError):
        WorkerPool(**{**values, "actions": ["shell"]}, image=pool.image)

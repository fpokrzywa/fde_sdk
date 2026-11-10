from datetime import timedelta
from uuid import uuid4

from shifde_contracts.common import utcnow
from shifde_contracts.jobs import ExecutionJob, JobLease
from shifde_executor.config import ExecutorSettings


def test_completed_receipt_survives_disconnect_and_replays_exact_checkpoint(tmp_path):
    from shifde_executor.receipts import ReceiptOutbox

    tenant, pool, worker = uuid4(), uuid4(), uuid4()
    image = "example/executor@sha256:" + "a" * 64
    settings = ExecutorSettings(
        gateway_url="https://delivery.test",
        token_file=tmp_path / "token",
        tenant_id=tenant,
        pool_id=pool,
        worker_id=worker,
        image=image,
        isolation="development",
        state_dir=tmp_path / "receipts",
    )
    job = ExecutionJob(
        tenant_id=tenant,
        operation_id=uuid4(),
        execution_id=uuid4(),
        operation_fence=1,
        pool_id=pool,
        action="apply",
        image=image,
        request={},
        toolchain_digest="a" * 64,
        deadline=utcnow() + timedelta(minutes=1),
    )
    lease = JobLease(
        job=job, lease_id=uuid4(), fence=1, expires_at=job.deadline, request_id=uuid4()
    )
    checkpoint = {
        "sequence": 2,
        "phase": "complete",
        "step": "bundle-deploy",
        "data": {"native_id": "native-app-123"},
    }
    first = ReceiptOutbox(settings)
    first.remember(lease, checkpoint)
    calls = []

    class Disconnected:
        def replay_checkpoint(self, job_id, body):
            calls.append((job_id, body))
            raise RuntimeError("disconnected")

    assert first.replay(Disconnected()) == 1
    restored = ReceiptOutbox(settings)

    class Connected:
        def replay_checkpoint(self, job_id, body):
            calls.append((job_id, body))
            return {"accepted": True}

    assert restored.replay(Connected()) == 0
    assert calls[0] == calls[1]
    assert calls[0][1]["lease_id"] == str(lease.lease_id)
    assert not list(settings.state_dir.rglob("*.json"))

from datetime import timedelta
from uuid import uuid4

import pytest
from shifde_contracts.common import utcnow
from shifde_contracts.jobs import ExecutionJob, JobLease
from shifde_executor.config import ExecutorSettings
from shifde_executor.supervisor import Supervisor


@pytest.mark.parametrize("cancelled", [False, True])
def test_pending_completion_is_kept_before_stop_and_blocks_terminal_result(tmp_path, cancelled):
    tenant, pool, worker = uuid4(), uuid4(), uuid4()
    settings = ExecutorSettings(
        gateway_url="https://delivery.test",
        token_file=tmp_path / "identity",
        tenant_id=tenant,
        pool_id=pool,
        worker_id=worker,
        image="example/worker@sha256:" + "a" * 64,
        isolation="development",
        state_dir=tmp_path / "state",
    )
    job = ExecutionJob(
        tenant_id=tenant,
        operation_id=uuid4(),
        execution_id=uuid4(),
        operation_fence=1,
        pool_id=pool,
        action="apply",
        image=settings.image,
        request={},
        toolchain_digest="a" * 64,
        deadline=utcnow() + timedelta(minutes=1),
    )
    lease = JobLease(
        job=job, lease_id=uuid4(), fence=1, expires_at=job.deadline, request_id=uuid4()
    )
    calls = []

    class Client:
        def __init__(self, initial):
            self.current = initial

        def lease(self):
            value, self.current = self.current, None
            return value

        def call(self, lease, method, body=None):
            calls.append(method)
            raise RuntimeError("gateway temporarily disconnected")

        def replay_checkpoint(self, identity, body):
            calls.append("replayed")
            return {"accepted": True}

    class Sandbox:
        def run(self, job, handler, *, stop=None):
            if cancelled:
                stop.set()
            handler(
                "checkpoint",
                {
                    "sequence": 2,
                    "phase": "complete",
                    "step": "bundle-deploy",
                    "data": {"native_id": "native-123"},
                },
            )

    supervisor = Supervisor(settings, client=Client(lease), sandbox=Sandbox())
    assert supervisor.run_once()
    assert "results" not in calls
    assert list(settings.state_dir.rglob("*.json"))
    assert not supervisor.run_once()
    assert "replayed" in calls and not list(settings.state_dir.rglob("*.json"))


def test_directory_sync_failure_cannot_acknowledge_receipt(tmp_path, monkeypatch):
    from shifde_executor import receipts

    settings = ExecutorSettings(
        gateway_url="https://delivery.test",
        token_file=tmp_path / "token",
        tenant_id=uuid4(),
        pool_id=uuid4(),
        worker_id=uuid4(),
        image="example/worker@sha256:" + "a" * 64,
        isolation="development",
        state_dir=tmp_path / "state",
    )
    outbox = receipts.ReceiptOutbox(settings)

    class Lease:
        lease_id = uuid4()
        fence = 1

        class job:
            id = uuid4()

    def failed_sync(path):
        raise OSError("simulated persistence failure")

    monkeypatch.setattr(receipts, "sync_directory", failed_sync, raising=False)
    with pytest.raises(OSError, match="simulated persistence failure"):
        outbox.remember(
            Lease(),
            {
                "sequence": 2,
                "phase": "complete",
                "step": "bundle-deploy",
                "data": {"native_id": "native-123"},
            },
        )

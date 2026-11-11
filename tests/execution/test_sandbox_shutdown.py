import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest
from shifde_contracts.common import utcnow
from shifde_contracts.jobs import ExecutionJob
from shifde_executor.config import ExecutorSettings
from shifde_executor.sandbox import DockerSandbox


@pytest.fixture
def process_sandbox(tmp_path):
    settings = ExecutorSettings(
        gateway_url="https://delivery.test",
        token_file=tmp_path / "token",
        tenant_id=uuid4(),
        pool_id=uuid4(),
        worker_id=uuid4(),
        image="example/executor@sha256:" + "a" * 64,
        isolation="development",
        max_job_seconds=20,
    )
    job = ExecutionJob(
        tenant_id=settings.tenant_id,
        operation_id=uuid4(),
        execution_id=uuid4(),
        operation_fence=1,
        pool_id=settings.pool_id,
        action="scaffold",
        image=settings.image,
        request={},
        toolchain_digest="a" * 64,
        deadline=utcnow() + timedelta(minutes=1),
    )
    script = tmp_path / "runner.py"

    class ProcessSandbox(DockerSandbox):
        result_ready = tmp_path / "result-ready"

        def command(self, job, name):
            return [sys.executable, "-u", str(script)]

        def _remove(self, name):
            pass

    def start(*, delay, exit_code=0, budget=20):
        script.write_text(
            "import json, sys, time\n"
            "from pathlib import Path\n"
            "sys.stdin.readline()\n"
            "print(json.dumps({'result': {'status': 'succeeded', 'value': {}}}), flush=True)\n"
            f"Path({str(ProcessSandbox.result_ready)!r}).write_text('ready')\n"
            f"time.sleep({delay!r})\n"
            f"sys.exit({exit_code!r})\n"
        )
        return ProcessSandbox(settings.model_copy(update={"max_job_seconds": budget})), job

    return start


def test_successful_shutdown_can_use_remaining_job_budget(process_sandbox):
    sandbox, job = process_sandbox(delay=11)
    assert sandbox.run(job, lambda *_: None) == {"status": "succeeded", "value": {}}


def test_success_frame_does_not_hide_nonzero_process_exit(process_sandbox):
    sandbox, job = process_sandbox(delay=0, exit_code=9)
    with pytest.raises(RuntimeError, match="sandbox failed"):
        sandbox.run(job, lambda *_: None)


@pytest.mark.parametrize("operation_deadline", [False, True])
def test_success_frame_does_not_extend_job_deadline(process_sandbox, operation_deadline):
    sandbox, job = process_sandbox(delay=30, budget=1)
    if operation_deadline:
        sandbox.settings = sandbox.settings.model_copy(update={"max_job_seconds": 20})
        job = job.model_copy(update={"deadline": utcnow() + timedelta(seconds=1)})
    started = time.monotonic()
    with pytest.raises((RuntimeError, TimeoutError)):
        sandbox.run(job, lambda *_: None)
    assert time.monotonic() - started < 5


def test_cancellation_still_stops_process_after_success_frame(process_sandbox):
    sandbox, job = process_sandbox(delay=30)
    stop = threading.Event()
    with ThreadPoolExecutor(max_workers=1) as executor:
        finished = executor.submit(sandbox.run, job, lambda *_: None, stop=stop)
        until = time.monotonic() + 5
        while not sandbox.result_ready.exists():
            if finished.done():
                finished.result()
            assert time.monotonic() < until, "runner did not produce its result"
            time.sleep(0.01)
        stop.set()
        with pytest.raises(RuntimeError, match="sandbox failed"):
            finished.result(timeout=5)

import base64
import json
import os
import subprocess
import time
from datetime import timedelta
from uuid import uuid4

import pytest
from shifde_contracts.common import SourceArchive, utcnow
from shifde_contracts.jobs import ExecutionJob
from shifde_executor.config import ExecutorSettings
from shifde_executor.sandbox import DockerSandbox

pytestmark = [pytest.mark.integration, pytest.mark.container]


@pytest.fixture
def sandbox_setup(tmp_path):
    image = os.getenv("SHIFDE_TEST_EXECUTOR_IMAGE")
    if not image:
        pytest.skip("set SHIFDE_TEST_EXECUTOR_IMAGE to a built digest-pinned executor image")
    tenant, pool = uuid4(), uuid4()
    settings = ExecutorSettings(
        gateway_url="https://delivery.test",
        token_file=tmp_path / "identity",
        tenant_id=tenant,
        pool_id=pool,
        worker_id=uuid4(),
        image=image,
        isolation="development",
        max_job_seconds=20,
    )
    job = ExecutionJob(
        tenant_id=tenant,
        operation_id=uuid4(),
        execution_id=uuid4(),
        operation_fence=1,
        pool_id=pool,
        action="scaffold",
        image=image,
        request={"spec": {"kind": "databricks.app/v1"}, "template_version": "0.1.0"},
        toolchain_digest="a" * 64,
        deadline=utcnow() + timedelta(minutes=1),
    )
    return settings, job


def test_real_container_is_nonroot_readonly_networkless_and_has_no_secrets(
    sandbox_setup, monkeypatch
):
    settings, job = sandbox_setup
    monkeypatch.setenv("DATABRICKS_TOKEN", "host-secret-must-not-enter-container")
    monkeypatch.setenv("SHIFDE_DATABASE_URL", "host-database-secret")
    seen = []

    def handle(method, data):
        assert method == "put"
        names = (
            subprocess.check_output(
                [
                    "docker",
                    "ps",
                    "--filter",
                    "name=shifde-job-" + job.id.hex,
                    "--format",
                    "{{.Names}}",
                ],
                text=True,
            )
            .strip()
            .splitlines()
        )
        assert len(names) == 1
        info = json.loads(subprocess.check_output(["docker", "inspect", names[0]], text=True))[0]
        assert info["HostConfig"]["ReadonlyRootfs"] and info["HostConfig"]["NetworkMode"] == "none"
        assert info["HostConfig"]["CapDrop"] == ["ALL"] and info["Config"]["User"] == "10001:10001"
        assert info["HostConfig"]["LogConfig"]["Type"] == "none"
        assert not [mount for mount in info["Mounts"] if mount["Type"] == "bind"]
        assert "host-secret" not in json.dumps(info) and "host-database-secret" not in json.dumps(
            info
        )
        probe = "import os,socket,pathlib,importlib.util; assert os.getuid()==10001; assert 'DATABRICKS_TOKEN' not in os.environ; assert importlib.util.find_spec('sqlalchemy') is None; assert importlib.util.find_spec('temporalio') is None; assert not pathlib.Path('/var/run/docker.sock').exists(); s=socket.socket(); s.settimeout(.5); assert s.connect_ex(('1.1.1.1',443))!=0; print('confined')"
        assert (
            subprocess.check_output(
                ["docker", "exec", names[0], "/opt/venv/bin/python", "-c", probe], text=True
            ).strip()
            == "confined"
        )
        durability = """
import os, stat, tempfile
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
from shifde_executor.receipts import ReceiptOutbox
events=[]
original=os.fsync
def observed(fd):
    events.append('directory' if stat.S_ISDIR(os.fstat(fd).st_mode) else 'file')
    return original(fd)
os.fsync=observed
settings=SimpleNamespace(state_dir=Path(tempfile.mkdtemp())/'receipts', tenant_id=uuid4(), pool_id=uuid4(), worker_id=uuid4())
outbox=ReceiptOutbox(settings)
events.clear()
lease=SimpleNamespace(lease_id=uuid4(), fence=1, job=SimpleNamespace(id=uuid4()))
path=outbox.remember(lease, {'sequence':2,'phase':'complete','step':'bundle-deploy','data':{'native_id':'test-native'}})
assert events==['file','directory'], events
outbox.acknowledge(path)
assert events[-1]=='directory' and not path.exists()
print('durable')
"""
        assert (
            subprocess.check_output(
                ["docker", "exec", names[0], "/opt/venv/bin/python", "-c", durability], text=True
            ).strip()
            == "durable"
        )
        raw = base64.b64decode(data["bytes"], validate=True)
        import hashlib

        ref = SourceArchive(
            tenant_id=job.tenant_id, digest=hashlib.sha256(raw).hexdigest(), size=len(raw)
        )
        seen.append(ref)
        return ref.model_dump(mode="json")

    result = DockerSandbox(settings).run(job, handle)
    assert result["status"] == "succeeded" and result["value"] == seen[0].model_dump(mode="json")
    assert (
        subprocess.check_output(
            [
                "docker",
                "ps",
                "-a",
                "--filter",
                "name=shifde-job-" + job.id.hex,
                "--format",
                "{{.Names}}",
            ],
            text=True,
        ).strip()
        == ""
    )


def test_container_timeout_terminates_job_and_removes_its_container(sandbox_setup):
    settings, job = sandbox_setup

    class HangingSandbox(DockerSandbox):
        def command(self, job, name):
            return [*super().command(job, name)[:-2], "-c", "import time; time.sleep(120)"]

    start = time.monotonic()
    with pytest.raises(RuntimeError, match="sandbox protocol failed"):
        HangingSandbox(settings.model_copy(update={"max_job_seconds": 1})).run(job, lambda *_: None)
    assert time.monotonic() - start < 20
    assert (
        subprocess.check_output(
            [
                "docker",
                "ps",
                "-a",
                "--filter",
                "name=shifde-job-" + job.id.hex,
                "--format",
                "{{.Names}}",
            ],
            text=True,
        ).strip()
        == ""
    )

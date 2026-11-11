import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import pytest
from shifde_executor.sandbox import DockerSandbox
from shifde_hosted.persistence import models as m

from tests.execution.test_private_worker import outbound_setup as outbound_setup
from tests.execution.test_snowflake_hosted import delivery_flow
from tests.execution.test_snowflake_hosted import snowflake_hosted as snowflake_hosted

pytestmark = [pytest.mark.integration, pytest.mark.container]


@pytest.fixture
def native_container_supervisor(snowflake_hosted, tmp_path):
    base = os.getenv("SHIFDE_TEST_EXECUTOR_IMAGE")
    if not base:
        pytest.skip("requires the current Snowflake-enabled executor image")
    _, _, supervisor, gateway, worker, pool, service, actor, _, vendor, _ = snowflake_hosted
    # Keep the native job deadline inside the driver's budget so a failed harness
    # cannot leave an orphan coordinator polling a leased job for an hour.
    service.operation_timeout_seconds = 180
    nonce = uuid4().hex
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            if not self.path.startswith("/" + nonce + "/"):
                self.send_error(404)
                return
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 32 * 1024 * 1024:
                self.send_error(413)
                return
            data = json.loads(self.rfile.read(size))
            method = self.path.rsplit("/", 1)[-1]
            try:
                with lock:
                    if method == "snapshot":
                        result = asyncio.run(vendor.snapshot(data["names"]))
                    elif method == "execute":
                        result = asyncio.run(
                            vendor.execute(data["sql"], warehouse=data.get("warehouse"))
                        )
                    elif method == "upload":
                        asyncio.run(
                            vendor.upload(
                                {k: base64.b64decode(v) for k, v in data["files"].items()},
                                data["location"],
                            )
                        )
                        result = {}
                    elif method == "download":
                        files = asyncio.run(vendor.download(data["names"], data["location"]))
                        result = {k: base64.b64encode(v).decode() for k, v in files.items()}
                    else:
                        raise ValueError("unknown fake method")
                payload = json.dumps(result).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except Exception:
                self.send_error(500)

    server = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    build = tmp_path / "image"
    build.mkdir()
    shutil.copyfile(
        Path(__file__).with_name("snowflake_container_runner.py"), build / "native_test.py"
    )
    (build / "vendor-url").write_text(f"http://host.docker.internal:{server.server_port}/{nonce}")
    base_tag = "shifde-sf-test-base:" + nonce
    subprocess.run(["docker", "tag", base, base_tag], check=True, timeout=20)
    (build / "Dockerfile").write_text(
        f"FROM {base_tag}\nUSER 0\nCOPY native_test.py vendor-url /opt/test/\nUSER 10001:10001\n"
    )
    image_tag, network = "shifde-snowflake-e2e:" + nonce, "shifde-snowflake-" + nonce
    try:
        built = subprocess.run(
            ["docker", "build", "-t", image_tag, str(build)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert built.returncode == 0, built.stderr[-2000:]
        metadata = json.loads(subprocess.check_output(["docker", "image", "inspect", image_tag]))[0]
        if not metadata["RepoDigests"]:
            local_tag = "localhost:5000/shifde-snowflake-test:" + nonce
            subprocess.run(["docker", "tag", image_tag, local_tag], check=True)
            subprocess.run(
                ["docker", "push", local_tag],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=120,
            )
            metadata = json.loads(
                subprocess.check_output(["docker", "image", "inspect", local_tag])
            )[0]
        image = metadata["RepoDigests"][0]
        subprocess.run(
            ["docker", "network", "create", network],
            check=True,
            stdout=subprocess.DEVNULL,
            timeout=20,
        )
        updated = pool.model_copy(update={"image": image})
        with service.database.transaction(actor.tenant_id) as c:
            c.execute(
                m.worker_pools.update()
                .where(m.worker_pools.c.id == pool.id)
                .values(data=updated.model_dump(mode="json"))
            )
        settings = supervisor.settings.model_copy(
            update={
                "image": image,
                "native_network": network,
                "heartbeat_seconds": 2,
                "state_dir": tmp_path / "receipts",
                "max_job_seconds": 150,
            }
        )
        supervisor.settings, supervisor.client.settings = settings, settings
        from shifde_executor.receipts import ReceiptOutbox

        supervisor.receipts = ReceiptOutbox(settings)

        class TestBoundarySandbox(DockerSandbox):
            def command(self, job, name):
                argv = super().command(job, name)
                if sys.platform == "linux":
                    pos = argv.index("--entrypoint")
                    argv[pos:pos] = ["--add-host", "host.docker.internal:host-gateway"]
                return [*argv[:-2], "/opt/test/native_test.py"]

        supervisor.sandbox = TestBoundarySandbox(settings)
        yield snowflake_hosted
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        subprocess.run(
            ["docker", "network", "rm", network],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
        )


async def test_hosted_native_app_create_patch_and_upgrade_in_fresh_containers(
    native_container_supervisor,
):
    await delivery_flow(native_container_supervisor, timeout=240)

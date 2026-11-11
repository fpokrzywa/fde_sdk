import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from shifde_executor.receipts import ReceiptOutbox
from shifde_executor.sandbox import DockerSandbox
from shifde_hosted.persistence import models as m

from tests.execution.native_http_boundary import BOUNDARY_HOST, BOUNDARY_PORT, native_http_relay
from tests.execution.test_palantir_hosted import palantir_flow
from tests.execution.test_palantir_hosted import palantir_hosted as palantir_hosted
from tests.execution.test_private_worker import outbound_setup as outbound_setup

pytestmark = [pytest.mark.integration, pytest.mark.container]


@pytest.fixture
def palantir_containers(palantir_hosted, tmp_path):
    base = os.getenv("SHIFDE_TEST_EXECUTOR_IMAGE")
    if not base:
        pytest.skip("requires the current Palantir-enabled executor image")
    _, _, supervisor, _, _, pool, service, actor, _, vendor, *_ = palantir_hosted
    nonce = uuid4().hex
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def handle_native(self):
            prefix = "/" + nonce
            if not self.path.startswith(prefix + "/"):
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= 32 * 1024 * 1024:
                self.send_error(413)
                return
            request = httpx.Request(
                self.command,
                self.headers["X-Native-Origin"] + self.path[len(prefix) :],
                content=self.rfile.read(length),
                headers={"Authorization": "Bearer boundary-only"}
                if self.headers.get("X-Native-Auth") == "yes"
                else {},
            )
            try:
                with lock:
                    response = vendor.handle(request)
                self.send_response(response.status_code)
                for key, value in response.headers.items():
                    if key.lower() != "content-length":
                        self.send_header(key, value)
                self.send_header("Content-Length", str(len(response.content)))
                self.end_headers()
                self.wfile.write(response.content)
            except Exception:
                self.send_error(500)

        do_POST = do_GET = do_PATCH = handle_native

    server = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    context = tmp_path / "image"
    context.mkdir()
    shutil.copyfile(
        Path(__file__).with_name("palantir_container_runner.py"), context / "native_test.py"
    )
    (context / "vendor-url").write_text(f"http://{BOUNDARY_HOST}:{BOUNDARY_PORT}/{nonce}")
    base_tag, image_tag = "shifde-palantir-test-base:" + nonce, "shifde-palantir-e2e:" + nonce
    network = "shifde-palantir-" + nonce
    try:
        subprocess.run(["docker", "tag", base, base_tag], check=True, timeout=20)
        (context / "Dockerfile").write_text(
            f"FROM {base_tag}\nUSER 0\nCOPY native_test.py vendor-url /opt/test/\nUSER 10001:10001\n"
        )
        result = subprocess.run(
            ["docker", "build", "-t", image_tag, str(context)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr[-2000:]
        metadata = json.loads(subprocess.check_output(["docker", "image", "inspect", image_tag]))[0]
        if not metadata["RepoDigests"]:
            local_tag = "localhost:5000/shifde-palantir-test:" + nonce
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
        pool = pool.model_copy(update={"image": image, "mode": "hosted"})
        with service.database.transaction(actor.tenant_id) as c:
            c.execute(
                m.worker_pools.update()
                .where(m.worker_pools.c.id == pool.id)
                .values(data=pool.model_dump(mode="json"))
            )
        settings = supervisor.settings.model_copy(
            update={"image": image, "native_network": network}
        )
        supervisor.settings, supervisor.client.settings = settings, settings
        supervisor.receipts = ReceiptOutbox(settings)

        class TestBoundarySandbox(DockerSandbox):
            def command(self, job, name):
                argv = super().command(job, name)
                return [*argv[:-2], "/opt/test/native_test.py"]

        supervisor.sandbox = TestBoundarySandbox(settings)
        with native_http_relay(base, network, server.server_port):
            yield palantir_hosted
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


async def test_palantir_create_update_through_hosted_api_and_fresh_containers(palantir_containers):
    await palantir_flow(palantir_containers, timeout=180)

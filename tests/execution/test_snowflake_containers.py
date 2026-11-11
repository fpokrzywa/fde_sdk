import asyncio
import base64
import json
import os
import shutil
import subprocess
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import pytest
from shifde_executor.sandbox import DockerSandbox
from shifde_hosted.persistence import models as m

from tests.execution.native_http_boundary import BOUNDARY_HOST, BOUNDARY_PORT, native_http_relay
from tests.execution.test_private_worker import outbound_setup as outbound_setup
from tests.execution.test_snowflake_hosted import delivery_flow
from tests.execution.test_snowflake_hosted import snowflake_hosted as snowflake_hosted

pytestmark = [pytest.mark.integration, pytest.mark.container]


@pytest.fixture
def native_container_supervisor(snowflake_hosted, tmp_path, request):
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
    (build / "vendor-url").write_text(f"http://{BOUNDARY_HOST}:{BOUNDARY_PORT}/{nonce}")
    mode = getattr(request, "param", {})
    (build / "exit-delay").write_text(str(mode.get("exit_delay", 0)))
    (build / "reuse-schema-port").write_text("1" if mode.get("reuse_schema_port") else "0")
    base_tag = "shifde-sf-test-base:" + nonce
    subprocess.run(["docker", "tag", base, base_tag], check=True, timeout=20)
    (build / "Dockerfile").write_text(
        f"FROM {base_tag}\nUSER 0\nCOPY native_test.py vendor-url exit-delay reuse-schema-port /opt/test/\nUSER 10001:10001\n"
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
            def run(self, job, handler, *, stop=None):
                def traced_handler(method, data):
                    try:
                        return handler(method, data)
                    except Exception as exc:
                        print(
                            "Snowflake test RPC failure:",
                            method,
                            data.get("step"),
                            type(exc).__name__,
                        )
                        raise

                try:
                    result = super().run(job, traced_handler, stop=stop)
                except Exception as exc:
                    print(
                        "Snowflake test sandbox failure:",
                        json.dumps(
                            {
                                "exception_type": type(exc).__name__,
                                "frames": [
                                    [Path(frame.filename).name, frame.name, frame.lineno]
                                    for frame in traceback.extract_tb(exc.__traceback__)
                                ],
                            }
                        ),
                    )
                    raise
                if result["status"] != "succeeded":
                    print("Snowflake test runner failure:", json.dumps(result.get("value")))
                return result

            def command(self, job, name):
                argv = super().command(job, name)
                return [*argv[:-2], "/opt/test/native_test.py"]

        supervisor.sandbox = TestBoundarySandbox(settings)
        with native_http_relay(base, network, server.server_port):
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


@pytest.mark.parametrize(
    "native_container_supervisor",
    [
        pytest.param({}, id="normal"),
        pytest.param(
            {"exit_delay": 11, "reuse_schema_port": True},
            id="reused-tcp-tuple-and-delayed-exit",
        ),
    ],
    indirect=True,
)
async def test_hosted_native_app_create_patch_and_upgrade_in_fresh_containers(
    native_container_supervisor,
):
    await delivery_flow(native_container_supervisor, timeout=240)
    vendor = native_container_supervisor[-2]
    assert [m[2:] for m in vendor.mutations if m[0] == "version"] == [
        ("V1", 0),
        ("V1", 1),
        ("V2", 0),
    ]
    assert [m[0] for m in vendor.mutations if m[0] in {"install", "upgrade"}] == [
        "install",
        "upgrade",
        "upgrade",
    ]


def test_fresh_containers_can_reuse_native_http_client_tuple(native_container_supervisor):
    supervisor = native_container_supervisor[2]
    probe = """
import json,socket
from pathlib import Path
from urllib.parse import urlsplit
url=urlsplit(Path('/opt/test/vendor-url').read_text())
payload=json.dumps({'names':{'package':'TEST_PACKAGE','application':'TEST_APP'}}).encode()
request=(f'POST {url.path}/snapshot HTTP/1.1\\r\\nHost: {url.netloc}\\r\\nContent-Length: {len(payload)}\\r\\nConnection: close\\r\\n\\r\\n').encode()+payload
sock=socket.socket()
sock.settimeout(3)
sock.bind(('',41029))
stage='connect'
try:
 sock.connect((url.hostname,url.port))
 stage='send'
 sock.sendall(request)
 stage='read'
 response=b''
 while True:
  data=sock.recv(65536)
  if not data:break
  response+=data
 print(json.dumps({'address':sock.getsockname(),'status':response.splitlines()[0].decode()}),flush=True)
except Exception as exc:
 print(json.dumps({'address':sock.getsockname(),'error':type(exc).__name__,'stage':stage}),flush=True)
finally:sock.close()
"""
    responses = []
    for _ in range(2):
        name = "shifde-http-probe-" + uuid4().hex
        try:
            result = subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--name",
                    name,
                    "--network",
                    supervisor.settings.native_network,
                    "--read-only",
                    "--user",
                    "10001:10001",
                    "--cap-drop",
                    "ALL",
                    "--entrypoint",
                    "/opt/venv/bin/python",
                    supervisor.settings.image,
                    "-u",
                    "-c",
                    probe,
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=15,
            )
            response = json.loads(result.stdout)
            responses.append(response)
            assert response.get("status") == "HTTP/1.0 200 OK", response
        finally:
            subprocess.run(
                ["docker", "rm", "--force", name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=20,
            )
    assert responses[0]["address"] == responses[1]["address"]
    assert native_container_supervisor[-2].mutations == []

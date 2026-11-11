"""Test image entry point: replace only the native Snowflake boundary over local HTTP."""

import asyncio
import base64
import http.client
import json
import sys
import time
import traceback
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from shifde_contracts.jobs import ExecutionJob
from shifde_executor.runner import MAX_FRAME, RPC, run_job
from shifde_snowflake.adapter import SnowflakeAdapter


class FakeVendorClient:
    def __init__(self, rpc):
        self.rpc = rpc
        self.url = Path("/opt/test/vendor-url").read_text()
        self.reuse_schema_port = Path("/opt/test/reuse-schema-port").read_text() == "1"

    async def call(self, method, data):
        credential = self.rpc.call("credential")
        if "snowflake" not in credential:
            raise PermissionError("test native profile mismatch")

        def request():
            if (
                self.reuse_schema_port
                and method == "execute"
                and data["sql"].startswith("CREATE SCHEMA")
            ):
                # Reuse a TCP tuple across the initial and patch containers.
                # This makes the host-forwarding TIME_WAIT failure reproducible.
                url = urlsplit(self.url)
                connection = http.client.HTTPConnection(
                    url.hostname, url.port, timeout=10, source_address=("", 41039)
                )
                try:
                    connection.request(
                        "POST",
                        url.path + "/" + method,
                        json.dumps(data),
                        {"Content-Type": "application/json", "Connection": "close"},
                    )
                    response = connection.getresponse()
                    if response.status != 200:
                        raise RuntimeError("test native boundary rejected request")
                    return json.loads(response.read())
                finally:
                    connection.close()
            with httpx.Client(timeout=10, trust_env=False) as client:
                response = client.post(self.url + "/" + method, json=data)
                response.raise_for_status()
                return response.json()

        return await asyncio.to_thread(request)

    async def snapshot(self, names):
        return await self.call("snapshot", {"names": names})

    async def execute(self, sql, *, warehouse=None):
        return await self.call("execute", {"sql": sql, "warehouse": warehouse})

    async def upload(self, files, location):
        return await self.call(
            "upload",
            {
                "files": {k: base64.b64encode(v).decode() for k, v in files.items()},
                "location": location,
            },
        )

    async def download(self, names, location):
        result = await self.call("download", {"names": names, "location": location})
        return {k: base64.b64decode(v) for k, v in result.items()}


def main():
    try:
        job = ExecutionJob.model_validate_json(sys.stdin.readline(MAX_FRAME + 1))
        rpc = RPC()
        adapter = SnowflakeAdapter(client_factory=lambda _: FakeVendorClient(rpc))
        value = asyncio.run(run_job(job, rpc, adapter=adapter))
        print(json.dumps({"result": {"status": "succeeded", "value": value}}), flush=True)
        # Exercise an attached process that exits late after proving the patch result.
        # Its total lifetime remains inside the real job/operation deadline.
        if job.action == "apply" and job.request["artifact"]["spec"]["release_patch"] == 1:
            time.sleep(float(Path("/opt/test/exit-delay").read_text()))
    except Exception as exc:
        print(
            json.dumps(
                {
                    "result": {
                        "status": "failed",
                        "error_code": "EXECUTION_FAILED",
                        "value": {
                            "exception_type": type(exc).__name__,
                            "frames": [
                                [Path(frame.filename).name, frame.name, frame.lineno]
                                for frame in traceback.extract_tb(exc.__traceback__)
                            ],
                        },
                    }
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    main()

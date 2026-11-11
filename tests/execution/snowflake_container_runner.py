"""Test image entry point: replace only the native Snowflake boundary over local HTTP."""

import asyncio
import base64
import json
import sys
from pathlib import Path

import httpx
from shifde_contracts.jobs import ExecutionJob
from shifde_executor.runner import MAX_FRAME, RPC, run_job
from shifde_snowflake.adapter import SnowflakeAdapter


class FakeVendorClient:
    def __init__(self, rpc):
        self.rpc = rpc
        self.url = Path("/opt/test/vendor-url").read_text()

    async def call(self, method, data):
        credential = self.rpc.call("credential")
        if "snowflake" not in credential:
            raise PermissionError("test native profile mismatch")

        def request():
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
    except Exception:
        print(
            json.dumps({"result": {"status": "failed", "error_code": "EXECUTION_FAILED"}}),
            flush=True,
        )


if __name__ == "__main__":
    main()

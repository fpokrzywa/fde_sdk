"""Test image entry point: real Fabric client; only its HTTP boundary is replaced."""

import asyncio
import json
import sys
from pathlib import Path

import httpx
from shifde_contracts.jobs import ExecutionJob
from shifde_executor.runner import MAX_FRAME, RPC, run_job
from shifde_fabric.adapter import FabricAdapter, FabricIQAdapter
from shifde_fabric.clients import FabricClient


class NativeTestTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request):
        # Never forward the real native bearer to the test HTTP server.
        url = Path("/opt/test/vendor-url").read_text() + request.url.raw_path.decode()
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            response = await client.request(
                request.method,
                url,
                content=request.content,
                headers={"Content-Type": "application/json"},
            )
        return httpx.Response(
            response.status_code, headers=dict(response.headers), content=response.content
        )


def main():
    try:
        job = ExecutionJob.model_validate_json(sys.stdin.readline(MAX_FRAME + 1))
        rpc = RPC()
        iq = job.application_kind == "fabric.iq/v1"
        adapter = (FabricIQAdapter if iq else FabricAdapter)(
            client_factory=lambda target: FabricClient(
                target, lambda: rpc.call("credential"), transport=NativeTestTransport(), iq=iq
            )
        )
        value = asyncio.run(run_job(job, rpc, adapter=adapter))
        print(json.dumps({"result": {"status": "succeeded", "value": value}}), flush=True)
    except Exception:
        print(
            json.dumps({"result": {"status": "failed", "error_code": "EXECUTION_FAILED"}}),
            flush=True,
        )


if __name__ == "__main__":
    main()

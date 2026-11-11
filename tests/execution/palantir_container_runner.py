"""Test image entry point: real Fabric client; only its HTTP boundary is replaced."""

import asyncio
import json
import sys
from pathlib import Path

import httpx
from shifde_contracts.jobs import ExecutionJob
from shifde_executor.runner import MAX_FRAME, RPC, run_job
from shifde_palantir.adapter import PalantirMarketplaceAdapter
from shifde_palantir.clients import PalantirClient


class NativeTestTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request):
        # Never forward the real native bearer to the test HTTP server.
        url = Path("/opt/test/vendor-url").read_text() + request.url.raw_path.decode()
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            response = await client.request(
                request.method,
                url,
                content=request.content,
                headers={
                    "Content-Type": "application/json",
                    "X-Native-Origin": str(request.url.copy_with(path="/", query=None)).rstrip("/"),
                    "X-Native-Auth": "yes" if "authorization" in request.headers else "no",
                },
            )
        return httpx.Response(
            response.status_code, headers=dict(response.headers), content=response.content
        )


def main():
    try:
        job = ExecutionJob.model_validate_json(sys.stdin.readline(MAX_FRAME + 1))
        rpc = RPC()
        adapter = PalantirMarketplaceAdapter(
            client_factory=lambda target: PalantirClient(
                target, lambda: rpc.call("credential"), transport=NativeTestTransport()
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

from uuid import uuid4

import httpx
import pytest
from shifde_contracts.common import Target
from shifde_fabric.clients import FabricAPIError, FabricClient


def client(transport, *, iq=False, allow_iq=True, execution=False):
    target = Target(
        tenant_id=uuid4(), connection_id=uuid4(), host="https://api.fabric.microsoft.com"
    )
    return FabricClient(
        target,
        lambda: {
            "bearer": "private-token",
            "fabric": {
                "host": target.host,
                "allow_iq": allow_iq,
                "allow_notebook_execution": execution,
            },
        },
        transport=transport,
        iq=iq,
    )


@pytest.mark.parametrize("status", [302, 401, 403, 429, 500])
async def test_fabric_never_retries_native_mutations_or_follows_redirects(status):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(
            status, headers={"Location": "https://evil.example", "Retry-After": "0"}
        )

    with pytest.raises(FabricAPIError):
        await client(httpx.MockTransport(handle)).request("PATCH", "/v1/test", {})
    assert len(requests) == 1 and requests[0].url.host == "api.fabric.microsoft.com"


async def test_read_throttling_honors_retry_after_and_refreshes_auth(monkeypatch):
    pauses, requests = [], []

    async def pause(seconds):
        pauses.append(seconds)

    monkeypatch.setattr("shifde_fabric.clients.asyncio.sleep", pause)

    def handle(request):
        requests.append(request)
        return (
            httpx.Response(429, headers={"Retry-After": "2"})
            if len(requests) == 1
            else httpx.Response(200, json={"id": "ok"})
        )

    result = await client(httpx.MockTransport(handle)).request("GET", "/v1/test", read=True)
    assert result["body"]["id"] == "ok" and pauses == [2]


async def test_iq_feature_gate_and_execution_policy_prevent_network_calls():
    def forbidden(request):
        raise AssertionError("must fail before network access")

    transport = httpx.MockTransport(forbidden)
    with pytest.raises(PermissionError):
        await client(transport, iq=True, allow_iq=False).request("POST", "/v1/test", {})
    with pytest.raises(PermissionError):
        await client(transport).request("POST", "/v1/test", {}, execution=True)

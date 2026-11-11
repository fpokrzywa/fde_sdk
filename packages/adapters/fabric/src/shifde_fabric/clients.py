"""Bounded Fabric REST transport. Native mutation requests are never replayed."""

import asyncio
import json
from datetime import timedelta
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from shifde_contracts.common import utcnow
from shifde_core.journal import NeedsAttention

from shifde_fabric.source import COLLECTIONS, ORIGIN


class FabricAPIError(RuntimeError):
    def __init__(self, status):
        super().__init__(
            f"Fabric API request failed ({status}); inspect native request diagnostics"
        )
        self.status = status


class FabricClient:
    def __init__(self, target, credential_provider, *, transport=None, iq=False):
        if target.host != ORIGIN:
            raise ValueError("unsupported Fabric API origin")
        self.credential_provider, self.transport, self.iq = credential_provider, transport, iq
        self.deadline = utcnow() + timedelta(hours=1)

    def auth(self, *, execution=False):
        auth = self.credential_provider()
        profile = auth.get("fabric", {})
        if (
            profile.get("host") != ORIGIN
            or self.iq
            and not profile.get("allow_iq")
            or execution
            and not profile.get("allow_notebook_execution")
        ):
            raise PermissionError(
                "Fabric local credential profile does not authorize this operation"
            )
        return auth["bearer"]

    async def request(self, method, path, body=None, *, params=None, execution=False, read=False):
        if not path.startswith("/v1/") or ".." in path or "?" in path or "#" in path:
            raise ValueError("invalid canonical Fabric path")
        for attempt in range(4):
            if utcnow() >= self.deadline:
                raise TimeoutError("Fabric operation deadline exceeded")
            token = self.auth(execution=execution)
            async with httpx.AsyncClient(
                base_url=ORIGIN,
                timeout=httpx.Timeout(30, connect=5),
                follow_redirects=False,
                trust_env=False,
                transport=self.transport,
            ) as client:
                async with client.stream(
                    method,
                    path,
                    json=body,
                    params=params,
                    headers={"Authorization": "Bearer " + token},
                ) as response:
                    data = bytearray()
                    async for block in response.aiter_bytes():
                        data.extend(block)
                        if len(data) > 24 * 1024 * 1024:
                            raise ValueError("Fabric response exceeded its bound")
            if response.status_code == 429 and read and attempt < 3:
                await self.pause(response.headers.get("Retry-After", "1"))
                continue
            if response.status_code not in {200, 201, 202, 204}:
                raise FabricAPIError(response.status_code)
            operation = response.headers.get("x-ms-operation-id")
            if response.status_code == 202:
                if not operation and not execution:
                    raise NeedsAttention("Fabric accepted a request without an operation ID")
                operation = str(UUID(operation)) if operation else None
            return {
                "status": response.status_code,
                "body": json.loads(data) if data else {},
                "operation_id": operation,
                "retry_after": response.headers.get("Retry-After", "1"),
                "location": response.headers.get("Location", ""),
            }
        raise FabricAPIError(429)

    async def pause(self, value):
        try:
            seconds = max(0.0, float(value))
        except ValueError:
            raise ValueError("invalid Fabric Retry-After") from None
        if not seconds < 61 or utcnow() + timedelta(seconds=seconds) >= self.deadline:
            raise TimeoutError("Fabric retry delay exceeds operation budget")
        await asyncio.sleep(seconds)

    async def wait(self, accepted, *, result_required=True):
        if accepted["status"] != 202:
            return accepted["body"]
        identity = str(UUID(accepted["operation_id"]))
        delay = accepted.get("retry_after", "1")
        for _ in range(128):
            await self.pause(delay)
            result = await self.request("GET", f"/v1/operations/{identity}", read=True)
            status = result["body"].get("status")
            if status == "Succeeded":
                if not result_required:
                    return {}
                return (await self.request("GET", f"/v1/operations/{identity}/result", read=True))[
                    "body"
                ]
            if status in {"Failed", "Cancelled", "Canceled"}:
                raise RuntimeError("Fabric native operation did not succeed")
            if status not in {"NotStarted", "Running", "Undefined"}:
                raise NeedsAttention("Fabric operation returned an unknown state")
            delay = result["retry_after"]
        raise TimeoutError("Fabric operation polling limit exceeded")

    async def workspace(self, workspace):
        return (await self.request("GET", f"/v1/workspaces/{workspace}", read=True))["body"]

    async def items(self, workspace):
        result, token, seen = [], None, set()
        for _ in range(32):
            data = (
                await self.request(
                    "GET",
                    f"/v1/workspaces/{workspace}/items",
                    params={"continuationToken": token} if token else None,
                    read=True,
                )
            )["body"]
            result.extend(data["value"])
            token = data.get("continuationToken")
            if len(result) > 1000:
                raise ValueError("Fabric workspace inventory exceeded its bound")
            if not token:
                return result
            if token in seen:
                raise ValueError("Fabric pagination token repeated")
            seen.add(token)
        raise ValueError("Fabric pagination limit exceeded")

    async def item(self, workspace, item_type, identity):
        return (
            await self.request(
                "GET",
                f"/v1/workspaces/{workspace}/{COLLECTIONS[item_type]}/{UUID(identity)}",
                read=True,
            )
        )["body"]

    async def definition(self, workspace, item_type, identity):
        result = await self.request(
            "POST",
            f"/v1/workspaces/{workspace}/{COLLECTIONS[item_type]}/{UUID(identity)}/getDefinition",
            params={"format": "ipynb"} if item_type == "Notebook" else None,
            read=True,
        )
        return (await self.wait(result))["definition"]

    @staticmethod
    def job_id(location, workspace, identity):
        parsed = urlsplit(location)
        prefix = f"/v1/workspaces/{workspace}/items/{identity}/jobs/instances/"
        if (
            parsed.scheme != "https"
            or parsed.netloc != "api.fabric.microsoft.com"
            or parsed.query
            or parsed.fragment
            or not parsed.path.startswith(prefix)
        ):
            raise NeedsAttention("Fabric job receipt has an unexpected Location")
        return str(UUID(parsed.path.removeprefix(prefix)))

"""Bounded Foundry REST preview client; no source scripts or CLI execution."""

import hashlib
from urllib.parse import quote

import httpx
from shifde_contracts.common import utcnow
from shifde_core.hashing import digest

from shifde_palantir.source import bindings


class PalantirClient:
    def __init__(self, target, credential_provider, *, transport=None):
        self.target, self.credential_provider, self.transport = (
            target,
            credential_provider,
            transport,
        )
        self.deadline = None

    def auth(self):
        if self.deadline and utcnow() >= self.deadline:
            raise TimeoutError("Foundry execution deadline exceeded")
        credential = self.credential_provider()
        profile = credential.get("palantir", {})
        if profile.get("host") != self.target.host or not profile.get("allow_marketplace_preview"):
            raise PermissionError("Foundry Marketplace preview is not enabled locally")
        if not credential.get("bearer"):
            raise PermissionError("Foundry native credential unavailable")
        return credential

    def approve(self, spec):
        b = bindings(spec, self.target)
        wanted = {
            **b,
            "version_id": str(spec.version_id),
            "object_type": spec.object_type,
            "frontend_asset_path": spec.frontend_asset_path,
            "frontend_asset_sha256": spec.frontend_asset_sha256,
        }
        if wanted not in self.auth()["palantir"].get("approved_releases", []):
            raise PermissionError(
                "Foundry release and verification bindings require local approval"
            )
        return b

    async def _request(self, method, url, *, headers=None, params=None, body=None):
        # No redirects, ambient proxy credentials, mutation retries or unbounded responses.
        async with httpx.AsyncClient(
            transport=self.transport,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(30, connect=5),
        ) as client:
            async with client.stream(
                method, url, headers=headers, params=params, json=body
            ) as response:
                if response.status_code not in {200, 201}:
                    raise RuntimeError("Foundry request failed; inspect native audit logs")
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 2 * 1024 * 1024:
                        raise ValueError("Foundry response exceeded budget")
                return bytes(data)

    async def request(self, method, path, body=None, *, preview=True, params=None):
        if not path.startswith("/api/") or "?" in path or "#" in path or ".." in path:
            raise ValueError("invalid Foundry API path")
        import json

        result = await self._request(
            method,
            self.target.host + path,
            headers={"Authorization": "Bearer " + self.auth()["bearer"]},
            params={"preview": "true"} if preview else params,
            body=body,
        )
        value = json.loads(result)
        if not isinstance(value, dict):
            raise ValueError("invalid Foundry response")
        return value

    async def schema(self, spec):
        b = self.approve(spec)
        ontology = quote(b["ontology"], safe="")
        metadata = await self.request("GET", f"/api/v2/ontologies/{ontology}", preview=False)
        if metadata.get("rid") != b["ontology"]:
            raise ValueError("Foundry ontology identity changed")
        schema = await self.request(
            "GET",
            f"/api/v2/ontologies/{ontology}/objectTypes/{quote(spec.object_type, safe='')}",
            preview=False,
        )
        if schema.get("apiName") != spec.object_type:
            raise ValueError("Foundry object type identity changed")
        return digest(schema)

    async def verify_reads(self, spec):
        b = self.approve(spec)
        await self.schema(spec)
        objects = await self.request(
            "GET",
            f"/api/v2/ontologies/{quote(b['ontology'], safe='')}/objects/{quote(spec.object_type, safe='')}",
            preview=False,
            params={"pageSize": "1"},
        )
        if not isinstance(objects.get("data", []), list) or "totalCount" not in objects:
            raise ValueError("Foundry object read response invalid")
        # Separate unauthenticated request: bearer must never cross to the frontend origin.
        asset = await self._request(
            "GET", b["frontend_origin"] + "/" + quote(spec.frontend_asset_path, safe="/")
        )
        return hashlib.sha256(asset).hexdigest() == spec.frontend_asset_sha256

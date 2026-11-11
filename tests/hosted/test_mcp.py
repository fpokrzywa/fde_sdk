import json
import time

import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

pytestmark = pytest.mark.integration


async def test_mcp_http_uses_caller_identity_and_same_idempotent_service(hosted_service):
    from shifde_hosted.auth import JWTVerifier
    from shifde_mcp.server import create_hosted_app

    service, actor = hosted_service
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def verifier(audience):
        return JWTVerifier(
            issuer="https://issuer.test", audience=audience, keys={"key": private.public_key()}
        )

    def token(subject="owner", aud="https://delivery.test/mcp"):
        return jwt.encode(
            {
                "sub": subject,
                "tenant_id": str(actor.tenant_id),
                "iss": "https://issuer.test",
                "aud": aud,
                "iat": int(time.time()),
                "exp": int(time.time()) + 600,
                "scope": " ".join(actor.scopes),
            },
            private,
            algorithm="RS256",
            headers={"kid": "key"},
        )

    app = create_hosted_app(
        service,
        verifier("https://delivery.test"),
        verifier("https://delivery.test/mcp"),
        public_url="https://delivery.test",
    )
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            headers={"Authorization": "Bearer " + token()},
            timeout=30,
        ) as http:
            async with Client(
                streamable_http_client("https://delivery.test/mcp", http_client=http)
            ) as client:
                tools = await client.list_tools()
                assert {
                    "build_application",
                    "deploy_application",
                    "get_operation",
                    "platform_schema",
                } <= {tool.name for tool in tools.tools}
                foundry_schema = await client.call_tool(
                    "platform_schema", {"platform": "palantir-marketplace"}
                )
                assert not foundry_schema.is_error, foundry_schema.content
                schema = foundry_schema.structured_content or json.loads(
                    foundry_schema.content[0].text
                )
                assert schema["$ref"] == "#/$defs/FoundryMarketplaceSpec"
                created = await client.call_tool(
                    "create_application",
                    {"request": {"name": "mcp-app", "kind": "databricks.app/v1"}},
                )
                app_id = created.structured_content["id"]
                arguments = {
                    "request": {"application_id": app_id, "spec": {"kind": "databricks.app/v1"}},
                    "idempotency_key": "mcp-scaffold",
                }
                first = await client.call_tool("scaffold_project", arguments)
                second = await client.call_tool("scaffold_project", arguments)
                assert first.structured_content["id"] == second.structured_content["id"]
                operation = service.get_operation(actor, first.structured_content["id"])
                assert operation.state == "queued"
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            headers={"Authorization": "Bearer " + token("reader")},
            timeout=30,
        ) as http:
            async with Client(
                streamable_http_client("https://delivery.test/mcp", http_client=http)
            ) as client:
                denied = await client.call_tool(
                    "create_application",
                    {"request": {"name": "denied", "kind": "databricks.app/v1"}},
                )
                assert denied.is_error and "FORBIDDEN" in str(denied.content)
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app)) as http:
            response = await http.post(
                "https://delivery.test/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            )
            assert response.status_code == 401
            metadata = await http.get(
                "https://delivery.test/.well-known/oauth-protected-resource/mcp"
            )
            assert (
                metadata.status_code == 200
                and metadata.json()["resource"] == "https://delivery.test/mcp"
            )

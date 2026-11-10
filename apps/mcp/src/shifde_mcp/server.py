import os
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import anyio
import httpx
from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl
from shifde_api.main import create_app, service_from_settings, verifier_from_settings
from shifde_contracts.capabilities import CapabilityReport
from shifde_contracts.common import SourceArchive, Target
from shifde_contracts.hosted import (
    ApplicationSubmission,
    Connection,
    ConnectionSubmission,
    EventPage,
    InspectionSubmission,
    PolicyDecisionSubmission,
    ProjectSubmission,
    ScaffoldSubmission,
    SourceUpload,
    SourceUploadSubmission,
    TargetSubmission,
    ValidationSubmission,
)
from shifde_contracts.operations import Operation
from shifde_contracts.projects import Application, Project
from shifde_contracts.submissions import BuildSubmission, DeploymentSubmission, PlanSubmission
from shifde_hosted.auth import JWTVerifier, Principal
from shifde_hosted.config import Settings
from shifde_hosted.errors import ServiceError
from shifde_hosted.service import ApplicationService


class MCPTokenVerifier:
    def __init__(self, verifier: JWTVerifier, resource: str):
        self.verifier, self.resource = verifier, resource

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            actor, claims = await anyio.to_thread.run_sync(self.verifier.verify, token)
            return AccessToken(
                token=token,
                client_id=actor.subject,
                subject=actor.subject,
                scopes=sorted(actor.scopes),
                expires_at=int(claims["exp"]),
                resource=self.resource,
                claims=claims,
            )
        except ServiceError:
            return None


class DirectBackend:
    def __init__(self, service: ApplicationService):
        self.service = service

    async def call(self, method, body=None, *, key=None, identity=None, after=0, limit=100):
        token = get_access_token()
        if not token or not token.subject or not token.claims:
            raise ToolError("UNAUTHENTICATED")
        actor = Principal(UUID(token.claims["tenant_id"]), token.subject, frozenset(token.scopes))

        def invoke():
            if method in ("scaffold", "inspection", "validation", "build", "plan", "deployment"):
                return self.service.submit(actor, method, body, key)
            if method == "get_operation":
                return self.service.get_operation(actor, identity)
            if method == "get_events":
                return self.service.events(actor, identity, after=after, limit=limit)
            if method == "capabilities":
                return self.service.capabilities(actor, identity)
            if method == "platform_schema":
                return self.service.platform_schema(actor, identity)
            if method == "cancel":
                return self.service.cancel(actor, identity)
            if method == "resume":
                return self.service.resume(actor, identity)
            if method == "policy":
                return self.service.decide_policy(actor, body)
            functions: dict[str, Callable[..., Any]] = {
                "create_application": self.service.create_application,
                "create_project": self.service.create_project,
                "create_connection": self.service.create_connection,
                "create_target": self.service.create_target,
                "create_upload": self.service.create_upload,
            }
            if method == "complete_upload":
                return self.service.complete_upload(actor, identity)
            return functions[method](actor, body)

        try:
            return await anyio.to_thread.run_sync(invoke)
        except ServiceError as error:
            raise ToolError(error.code) from None
        except Exception:
            raise ToolError("INTERNAL_ERROR") from None


class HTTPBackend:
    """stdio only: an explicit caller token is forwarded to the hosted API."""

    def __init__(self, base_url: str, token_provider):
        self.base_url = Target._validate_host(base_url)
        self.token_provider = token_provider

    async def call(self, method, body=None, *, key=None, identity=None, after=0, limit=100):
        paths = {
            "scaffold": "scaffolds",
            "inspection": "inspections",
            "validation": "validations",
            "build": "builds",
            "plan": "plans",
            "deployment": "deployments",
            "create_application": "applications",
            "create_project": "projects",
            "create_connection": "connections",
            "create_target": "targets",
            "create_upload": "source-uploads",
            "complete_upload": "source-upload-completions",
            "cancel": "operation-cancellations",
            "resume": "operation-resumptions",
            "policy": "policy-decisions",
        }
        verb = "POST"
        payload = body.model_dump(mode="json") if body is not None else None
        if method == "get_operation":
            path = f"operations/{identity}"
            verb = "GET"
        elif method == "get_events":
            path = f"operations/{identity}/events?after={after}&limit={limit}"
            verb = "GET"
        elif method == "capabilities":
            path = f"targets/{identity}/capabilities"
            verb = "GET"
        elif method == "platform_schema":
            path = f"platform-schemas/{identity}"
            verb = "GET"
        else:
            path = paths[method]
        if method in ("cancel", "resume"):
            payload = {"operation_id": str(identity)}
        if method == "complete_upload":
            payload = {"upload_id": str(identity)}
        token = self.token_provider()
        headers = {"Authorization": "Bearer " + token}
        if key:
            headers["Idempotency-Key"] = key
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            response = await client.request(
                verb, self.base_url + "/v1/" + path, headers=headers, json=payload
            )
        if not response.is_success:
            raise ToolError("API_REQUEST_FAILED")
        return response.json()


def register_tools(server: MCPServer, backend):
    @server.tool()
    async def platform_capabilities(target_id: UUID) -> CapabilityReport:
        """Read cached target capabilities; inspect the environment to refresh them."""
        return CapabilityReport.model_validate(
            await backend.call("capabilities", identity=target_id)
        )

    @server.tool()
    async def inspect_environment(request: InspectionSubmission, idempotency_key: str) -> Operation:
        """Queue bounded native metadata inspection for a registered application and target."""
        return Operation.model_validate(
            await backend.call("inspection", request, key=idempotency_key)
        )

    @server.tool()
    async def scaffold_project(request: ScaffoldSubmission, idempotency_key: str) -> Operation:
        """Queue native source scaffolding without executing application code."""
        return Operation.model_validate(
            await backend.call("scaffold", request, key=idempotency_key)
        )

    @server.tool()
    async def validate_project(request: ValidationSubmission, idempotency_key: str) -> Operation:
        """Queue validation of an immutable project revision."""
        return Operation.model_validate(
            await backend.call("validation", request, key=idempotency_key)
        )

    @server.tool()
    async def build_application(request: BuildSubmission, idempotency_key: str) -> Operation:
        """Queue a build and return its durable operation ID."""
        return Operation.model_validate(await backend.call("build", request, key=idempotency_key))

    @server.tool()
    async def plan_deployment(request: PlanSubmission, idempotency_key: str) -> Operation:
        """Plan changes against an immutable artifact and registered target."""
        return Operation.model_validate(await backend.call("plan", request, key=idempotency_key))

    @server.tool()
    async def deploy_application(request: DeploymentSubmission, idempotency_key: str) -> Operation:
        """Submit a stored plan/artifact pair; native submission is not a health claim."""
        return Operation.model_validate(
            await backend.call("deployment", request, key=idempotency_key)
        )

    @server.tool()
    async def get_operation(operation_id: UUID) -> Operation:
        """Read durable status and typed results for an operation in the caller's tenant."""
        return Operation.model_validate(await backend.call("get_operation", identity=operation_id))

    @server.tool()
    async def get_operation_events(
        operation_id: UUID, after: int = 0, limit: int = 100
    ) -> EventPage:
        """Read ordered events after a cursor."""
        return EventPage.model_validate(
            await backend.call("get_events", identity=operation_id, after=after, limit=limit)
        )

    @server.tool()
    async def platform_schema(platform: str) -> dict:
        """Read the native input schema for databricks, palantir, snowflake or fabric."""
        return await backend.call("platform_schema", identity=platform)

    @server.tool()
    async def create_application(request: ApplicationSubmission) -> Application:
        """Register a stable application identity."""
        return Application.model_validate(await backend.call("create_application", request))

    @server.tool()
    async def create_project(request: ProjectSubmission) -> Project:
        """Register a project revision referencing an authorized source archive."""
        return Project.model_validate(await backend.call("create_project", request))

    @server.tool()
    async def create_connection(request: ConnectionSubmission) -> Connection:
        """Owner only: bind an operator-approved host and credential reference."""
        return Connection.model_validate(await backend.call("create_connection", request))

    @server.tool()
    async def create_target(request: TargetSubmission) -> Target:
        """Register environment bindings under an existing connection."""
        return Target.model_validate(await backend.call("create_target", request))

    @server.tool()
    async def create_source_upload(request: SourceUploadSubmission) -> SourceUpload:
        """Create a bounded upload grant; upload bytes through the authenticated HTTP content endpoint."""
        return SourceUpload.model_validate(await backend.call("create_upload", request))

    @server.tool()
    async def complete_source_upload(upload_id: UUID) -> SourceArchive:
        """Finalize an uploaded archive after its digest is verified."""
        return SourceArchive.model_validate(
            await backend.call("complete_upload", identity=upload_id)
        )

    @server.tool()
    async def cancel_operation(operation_id: UUID) -> Operation:
        """Request cooperative cancellation; this does not undo native effects."""
        return Operation.model_validate(await backend.call("cancel", identity=operation_id))

    @server.tool()
    async def resume_operation(operation_id: UUID) -> Operation:
        """Resume bookkeeping only when a durable native receipt proves the outcome."""
        return Operation.model_validate(await backend.call("resume", identity=operation_id))

    @server.tool()
    async def decide_deployment_policy(request: PolicyDecisionSubmission) -> Operation:
        """Approver only: record a decision bound to a waiting operation and sealed plan."""
        return Operation.model_validate(await backend.call("policy", request))


def create_hosted_app(
    service: ApplicationService,
    api_verifier: JWTVerifier,
    mcp_verifier: JWTVerifier,
    *,
    public_url: str,
):
    resource = public_url.rstrip("/") + "/mcp"
    server = MCPServer(
        "SHIFDE native application delivery",
        version="0.1.0",
        instructions="Use registered resources. Build before planning; deploy the stored artifact and plan. Poll operation IDs. Never retry ambiguous native writes.",
        token_verifier=MCPTokenVerifier(mcp_verifier, resource),
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(mcp_verifier.issuer),
            resource_server_url=AnyHttpUrl(resource),
            required_scopes=["shifde:read"],
            validate_token_resource=True,
        ),
    )
    register_tools(server, DirectBackend(service))
    origin = urlsplit(public_url)
    transport = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[origin.netloc],
        allowed_origins=[public_url.rstrip("/")],
    )
    mcp_app = server.streamable_http_app(
        json_response=True,
        stateless_http=True,
        transport_security=transport,
        max_request_body_size=1024 * 1024,
    )
    app = create_app(
        service, api_verifier, public_url=public_url, extra_lifespan=server.session_manager.run
    )
    app.mount("/", mcp_app)
    return app


def create_from_env():
    settings = Settings()
    return create_hosted_app(
        service_from_settings(settings),
        verifier_from_settings(settings, audience=settings.api_audience),
        verifier_from_settings(settings, audience=settings.mcp_audience),
        public_url=settings.public_url,
    )


def main():
    import sys

    if "--stdio" in sys.argv:
        server = MCPServer("SHIFDE API bridge", version="0.1.0")
        register_tools(
            server,
            HTTPBackend(os.environ["SHIFDE_API_URL"], lambda: os.environ["SHIFDE_CALLER_TOKEN"]),
        )
        server.run(transport="stdio")
    else:
        import uvicorn

        uvicorn.run("shifde_mcp.server:create_from_env", factory=True, host="0.0.0.0", port=8000)

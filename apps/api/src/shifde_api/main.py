import json
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Annotated
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import anyio
import jwt
from fastapi import Depends, FastAPI, Header, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from shifde_contracts.capabilities import CapabilityReport
from shifde_contracts.common import SourceArchive, Target
from shifde_contracts.hosted import (
    ApplicationSubmission,
    Connection,
    ConnectionSubmission,
    DeploymentView,
    EventPage,
    InspectionSubmission,
    OperationControl,
    PolicyDecisionSubmission,
    ProjectSubmission,
    ScaffoldSubmission,
    SourceUpload,
    SourceUploadCompletion,
    SourceUploadSubmission,
    TargetPatch,
    TargetSubmission,
    ValidationSubmission,
)
from shifde_contracts.operations import Operation
from shifde_contracts.projects import Application, Project
from shifde_contracts.submissions import BuildSubmission, DeploymentSubmission, PlanSubmission
from shifde_core.artifact_store import MAX_ARCHIVE_BYTES, LocalArtifactStore
from shifde_hosted.auth import JWTVerifier, Principal
from shifde_hosted.config import Settings
from shifde_hosted.errors import ServiceError
from shifde_hosted.persistence.database import Database
from shifde_hosted.service import MODEL_KINDS, ApplicationService
from starlette.middleware.trustedhost import TrustedHostMiddleware


def error_response(code: str, status: int, *, details=None):
    return JSONResponse(
        {
            "error": {
                "code": code,
                "message": "request could not be completed",
                "details": details or [],
                "trace_id": str(uuid4()),
            }
        },
        status_code=status,
        headers={"WWW-Authenticate": "Bearer"} if status == 401 else None,
    )


class BodyLimitMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = (
            MAX_ARCHIVE_BYTES
            if scope["path"].startswith("/v1/source-uploads/")
            and scope["path"].endswith("/content")
            else 1024 * 1024
        )
        headers = dict(scope["headers"])
        try:
            declared = int(headers.get(b"content-length", b"0"))
        except ValueError:
            return await error_response("INVALID_CONTENT_LENGTH", 400)(scope, receive, send)
        if declared > limit:
            return await error_response("BODY_TOO_LARGE", 413)(scope, receive, send)
        total = 0

        async def limited_receive():
            nonlocal total
            message = await receive()
            total += len(message.get("body", b""))
            if total > limit:
                raise ServiceError("BODY_TOO_LARGE", 413)
            return message

        await self.app(scope, limited_receive, send)


def create_app(
    service: ApplicationService, verifier: JWTVerifier, *, public_url: str, extra_lifespan=None
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        await anyio.to_thread.run_sync(service.database.check_runtime)
        async with AsyncExitStack() as stack:
            if extra_lifespan:
                await stack.enter_async_context(extra_lifespan())
            yield

    app = FastAPI(
        title="SHIFDE application delivery",
        version="0.1.0",
        lifespan=lifespan,
        responses={
            401: {"description": "Bearer identity required"},
            403: {"description": "Permission denied"},
            409: {"description": "State conflict"},
        },
    )
    app.state.service = service
    hostname = urlsplit(public_url).hostname
    if not hostname:
        raise ValueError("public_url requires a hostname")
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[hostname])
    app.add_middleware(BodyLimitMiddleware)
    bearer = HTTPBearer(auto_error=False)

    def identity(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> Principal:
        if not credentials:
            raise ServiceError("UNAUTHENTICATED", 401)
        return verifier.verify(credentials.credentials)[0]

    Actor = Annotated[Principal, Depends(identity)]
    Key = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=200)]

    @app.exception_handler(ServiceError)
    async def domain_error(request, error):
        return error_response(error.code, error.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        return error_response(
            "VALIDATION_FAILED",
            422,
            details=[
                {"field": ".".join(map(str, item["loc"])), "issue": item["type"]}
                for item in error.errors()
            ],
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request, error):
        return error_response("INTERNAL_ERROR", 500)

    @app.get("/health/live")
    def health():
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready():
        service.database.check_runtime()
        return {"status": "ok"}

    @app.post("/v1/applications", status_code=201, response_model=Application)
    def create_application(body: ApplicationSubmission, actor: Actor, response: Response):
        result = service.create_application(actor, body)
        response.headers["ETag"] = '"1"'
        return result

    @app.post("/v1/connections", status_code=201, response_model=Connection)
    def create_connection(body: ConnectionSubmission, actor: Actor, response: Response):
        result = service.create_connection(actor, body)
        response.headers["ETag"] = '"1"'
        return result

    @app.post("/v1/targets", status_code=201, response_model=Target)
    def create_target(body: TargetSubmission, actor: Actor, response: Response):
        result = service.create_target(actor, body)
        response.headers["ETag"] = '"1"'
        return result

    @app.patch("/v1/targets/{identity}", response_model=Target)
    def patch_target(
        identity: UUID,
        body: TargetPatch,
        actor: Actor,
        response: Response,
        if_match: Annotated[str | None, Header()] = None,
    ):
        if if_match is None:
            raise ServiceError("PRECONDITION_REQUIRED", 428)
        try:
            revision = int(if_match.strip('"'))
        except ValueError:
            raise ServiceError("INVALID_ETAG", 422) from None
        model, revision = service.patch_target(actor, identity, body, expected_revision=revision)
        response.headers["ETag"] = f'"{revision}"'
        return model

    @app.post("/v1/projects", status_code=201, response_model=Project)
    def create_project(body: ProjectSubmission, actor: Actor):
        return service.create_project(actor, body)

    @app.post("/v1/source-uploads", status_code=201, response_model=SourceUpload)
    def create_upload(body: SourceUploadSubmission, actor: Actor):
        return service.create_upload(actor, body)

    @app.put("/v1/source-uploads/{identity}/content", status_code=204)
    async def upload_content(identity: UUID, request: Request, actor: Actor):
        content = await request.body()
        await anyio.to_thread.run_sync(lambda: service.upload_content(actor, identity, content))
        return Response(status_code=204)

    @app.post("/v1/source-upload-completions", response_model=SourceArchive)
    def complete_upload(body: SourceUploadCompletion, actor: Actor):
        return service.complete_upload(actor, body.upload_id)

    @app.get("/v1/sources/{identity}/content")
    def source_content(identity: UUID, actor: Actor):
        return Response(
            service.source_content(actor, identity),
            media_type="application/zip",
            headers={"Content-Disposition": "attachment; filename=source.zip"},
        )

    def resource_route(kind, model):
        def get(identity: UUID, actor: Actor, response: Response):
            result, revision = service.get_resource(actor, kind, identity)
            response.headers["ETag"] = f'"{revision}"'
            return result

        plural = {
            "application": "applications",
            "connection": "connections",
            "target": "targets",
            "project": "projects",
            "source": "sources",
            "artifact": "artifacts",
            "plan": "plans",
        }[kind]
        app.add_api_route(
            f"/v1/{plural}/{{identity}}",
            get,
            methods=["GET"],
            response_model=model,
            operation_id=f"get_{kind}",
        )

    for kind, model in MODEL_KINDS.items():
        if kind != "deployment":
            resource_route(kind, model)

    def submit(actor, kind, body, key, response):
        op = service.submit(actor, kind, body, key)
        response.headers["Location"] = "/v1/operations/" + str(op.id)
        return op

    @app.post("/v1/scaffolds", status_code=202, response_model=Operation)
    def scaffolds(body: ScaffoldSubmission, actor: Actor, response: Response, key: Key):
        return submit(actor, "scaffold", body, key, response)

    @app.post("/v1/inspections", status_code=202, response_model=Operation)
    def inspections(body: InspectionSubmission, actor: Actor, response: Response, key: Key):
        return submit(actor, "inspection", body, key, response)

    @app.post("/v1/validations", status_code=202, response_model=Operation)
    def validations(body: ValidationSubmission, actor: Actor, response: Response, key: Key):
        return submit(actor, "validation", body, key, response)

    @app.post("/v1/builds", status_code=202, response_model=Operation)
    def builds(body: BuildSubmission, actor: Actor, response: Response, key: Key):
        return submit(actor, "build", body, key, response)

    @app.post("/v1/plans", status_code=202, response_model=Operation)
    def plans(body: PlanSubmission, actor: Actor, response: Response, key: Key):
        return submit(actor, "plan", body, key, response)

    @app.post("/v1/deployments", status_code=202, response_model=Operation)
    def deployments(body: DeploymentSubmission, actor: Actor, response: Response, key: Key):
        return submit(actor, "deployment", body, key, response)

    @app.get("/v1/operations/{identity}", response_model=Operation)
    def operation(identity: UUID, actor: Actor):
        return service.get_operation(actor, identity)

    @app.get("/v1/operations/{identity}/events", response_model=EventPage)
    def events(identity: UUID, actor: Actor, after: int = 0, limit: int = 100):
        return service.events(actor, identity, after=after, limit=limit)

    @app.get("/v1/deployments/{identity}", response_model=DeploymentView)
    def deployment(identity: UUID, actor: Actor):
        return service.deployment(actor, identity)

    @app.get("/v1/targets/{identity}/capabilities", response_model=CapabilityReport)
    def capabilities(identity: UUID, actor: Actor):
        return service.capabilities(actor, identity)

    @app.post("/v1/operation-cancellations", response_model=Operation)
    def cancel(body: OperationControl, actor: Actor):
        return service.cancel(actor, body.operation_id)

    @app.post("/v1/operation-resumptions", response_model=Operation)
    def resume(body: OperationControl, actor: Actor):
        return service.resume(actor, body.operation_id)

    @app.post("/v1/policy-decisions", response_model=Operation)
    def decide_policy(body: PolicyDecisionSubmission, actor: Actor):
        return service.decide_policy(actor, body)

    @app.get("/v1/platform-schemas/{platform}")
    def platform_schema(platform: str, actor: Actor):
        return service.platform_schema(actor, platform)

    return app


def verifier_from_settings(settings: Settings, *, audience: str) -> JWTVerifier:
    keys = None
    if settings.auth_jwks_file:
        document = json.loads(settings.auth_jwks_file.read_bytes())
        keys = {item["kid"]: jwt.PyJWK.from_dict(item).key for item in document["keys"]}
    return JWTVerifier(
        issuer=settings.auth_issuer, audience=audience, keys=keys, jwks_url=settings.auth_jwks_url
    )


def service_from_settings(settings: Settings) -> ApplicationService:
    from shifde_core.artifact_store import ArtifactStore

    database = Database(settings.database_url.get_secret_value())
    artifacts: ArtifactStore
    if settings.artifact_container_url:
        from shifde_hosted.storage import AzureArtifactStore

        artifacts = AzureArtifactStore(settings.artifact_container_url, settings.artifact_root)
    else:
        artifacts = LocalArtifactStore(settings.artifact_root)
    return ApplicationService(
        database,
        artifacts,
        credential_bindings=settings.credential_bindings,
        require_approval_environments=tuple(settings.require_approval_environments),
        operation_timeout_seconds=settings.operation_timeout_seconds,
    )


def create_from_env():
    settings = Settings()
    return create_app(
        service_from_settings(settings),
        verifier_from_settings(settings, audience=settings.api_audience),
        public_url=settings.public_url,
    )


def main():
    import uvicorn

    uvicorn.run("shifde_api.main:create_from_env", factory=True, host="0.0.0.0", port=8000)

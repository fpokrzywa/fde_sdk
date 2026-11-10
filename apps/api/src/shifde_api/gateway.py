from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from shifde_contracts.jobs import (
    CheckpointSubmission,
    CredentialGrant,
    JobLease,
    JobResultSubmission,
    LeaseProof,
    LeaseRequest,
)
from shifde_core.journal import NeedsAttention
from shifde_hosted.errors import ServiceError
from shifde_hosted.gateway import ExecutionGateway, WorkerPrincipal


def install_gateway(app, service, verifier):
    gateway = ExecutionGateway(service)
    bearer = HTTPBearer(auto_error=False)

    def worker(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
        if credentials is None:
            raise ServiceError("UNAUTHENTICATED", 401)
        principal, claims = verifier.verify(credentials.credentials)
        try:
            if "shifde:execute" not in principal.scopes:
                raise ValueError()
            return WorkerPrincipal(
                principal.tenant_id,
                UUID(claims["worker_pool_id"]),
                UUID(claims["executor_id"]),
                principal.subject,
            )
        except (KeyError, ValueError, TypeError):
            raise ServiceError("UNAUTHENTICATED", 401) from None

    Worker = Annotated[WorkerPrincipal, Depends(worker)]

    @app.post("/internal/v1/job-leases", response_model=JobLease | None)
    def lease(body: LeaseRequest, actor: Worker):
        return gateway.lease(actor, body)

    @app.post("/internal/v1/execution-jobs/{identity}/heartbeats")
    def heartbeat(identity: UUID, body: LeaseProof, actor: Worker):
        return gateway.heartbeat(actor, identity, body)

    @app.post("/internal/v1/execution-jobs/{identity}/guards")
    def guard(identity: UUID, body: LeaseProof, actor: Worker):
        return gateway.guard(actor, identity, body)

    @app.post(
        "/internal/v1/execution-jobs/{identity}/credential-grants", response_model=CredentialGrant
    )
    def credentials(identity: UUID, body: LeaseProof, actor: Worker):
        return gateway.credential_grant(actor, identity, body)

    @app.post("/internal/v1/execution-jobs/{identity}/checkpoints")
    def checkpoint(identity: UUID, body: CheckpointSubmission, actor: Worker):
        try:
            return gateway.checkpoint(actor, identity, body)
        except NeedsAttention:
            raise ServiceError("UNPROVEN_NATIVE_OUTCOME") from None

    @app.post("/internal/v1/execution-jobs/{identity}/results")
    def result(identity: UUID, body: JobResultSubmission, actor: Worker):
        return gateway.result(actor, identity, body)

    def proof(
        lease_id: Annotated[UUID, Header(alias="X-Lease-Id")],
        fence: Annotated[int, Header(alias="X-Execution-Fence", ge=1)],
    ):
        return LeaseProof(lease_id=lease_id, fence=fence)

    Proof = Annotated[LeaseProof, Depends(proof)]

    @app.get("/internal/v1/execution-jobs/{identity}/artifacts/{source_id}")
    def source(identity: UUID, source_id: UUID, actor: Worker, lease: Proof):
        return Response(
            gateway.input_content(actor, identity, lease, source_id), media_type="application/zip"
        )

    @app.post("/internal/v1/execution-jobs/{identity}/artifacts")
    async def output(identity: UUID, request: Request, actor: Worker, lease: Proof):
        import anyio

        data = await request.body()
        return await anyio.to_thread.run_sync(
            lambda: gateway.output_content(actor, identity, lease, data)
        )

import asyncio
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from shifde_contracts.submissions import DeploymentSubmission
from shifde_contracts.workflow import OperationRef
from shifde_executor.client import GatewayClient
from shifde_executor.config import ExecutorSettings, LocalCredentialBinding
from shifde_executor.runner import run_job
from shifde_executor.supervisor import Supervisor
from shifde_hosted.auth import JWTVerifier
from shifde_hosted.remote_adapter import GatewayAdapter

pytestmark = pytest.mark.integration


@pytest.fixture
def outbound_setup(gateway_setup, tmp_path, monkeypatch):
    from shifde_api.main import create_app
    from shifde_databricks.adapter import DatabricksAdapter

    gateway, worker, pool, setup = gateway_setup
    service, actor, executor, cli, workspace, *_ = setup
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setenv("TEST_SUPERVISOR_NATIVE_TOKEN", "native-only-local-test-secret")
    original_execute = cli.execute

    async def unique_start(action, *args, **kwargs):
        value = await original_execute(action, *args, **kwargs)
        if action == "run":
            from uuid import uuid4

            workspace.app["active_deployment"]["deployment_id"] = str(uuid4())
        return value

    cli.execute = unique_start

    def token(subject, audience, **extra):
        return jwt.encode(
            dict(
                sub=subject,
                tenant_id=str(actor.tenant_id),
                iss="https://issuer.test",
                aud=audience,
                iat=int(time.time()),
                exp=int(time.time()) + 600,
                scope=" ".join(actor.scopes | {"shifde:execute"}),
                **extra,
            ),
            private,
            algorithm="RS256",
            headers={"kid": "worker-test"},
        )

    token_file = tmp_path / "worker-token"
    token_file.write_text(
        token(
            worker.subject,
            "https://delivery.test/executor",
            worker_pool_id=str(worker.pool_id),
            executor_id=str(worker.worker_id),
        )
    )
    app = create_app(
        service,
        JWTVerifier(
            issuer="https://issuer.test",
            audience="https://delivery.test",
            keys={"worker-test": private.public_key()},
        ),
        public_url="https://delivery.test",
    )

    class BoundarySandbox:
        # Only the native vendor boundary is fake; run real adapter, journal and artifact RPC.
        def run(self, job, handler, *, stop=None):
            if job.target:
                assert handler("credential", {})["bearer"] == "native-only-local-test-secret"

            class RPC:
                def call(self, method, data=None):
                    return handler(method, data or {})

            adapter = DatabricksAdapter(cli=cli, workspace_factory=lambda _: workspace)
            return {
                "status": "succeeded",
                "value": asyncio.run(run_job(job, RPC(), adapter=adapter)),
            }

    with TestClient(app, base_url="https://delivery.test") as server:

        def transport(request):
            response = server.request(
                request.method,
                str(request.url),
                headers=dict(request.headers),
                content=request.content,
            )
            return httpx.Response(
                response.status_code, headers=response.headers, content=response.content
            )

        settings = ExecutorSettings(
            gateway_url="https://delivery.test",
            token_file=token_file,
            tenant_id=actor.tenant_id,
            pool_id=pool.id,
            worker_id=worker.worker_id,
            image=pool.image,
            isolation="development",
            heartbeat_seconds=0.2,
            credential_bindings={
                "development": LocalCredentialBinding(
                    tenant_id=actor.tenant_id,
                    host=service.credential_bindings["development"].host,
                    mode="token_env",
                    token_env="TEST_SUPERVISOR_NATIVE_TOKEN",
                )
            },
        )
        client = GatewayClient(settings, transport=httpx.MockTransport(transport))
        supervisor = Supervisor(settings, client=client, sandbox=BoundarySandbox())
        executor.adapter_factory = lambda _: GatewayAdapter(gateway, poll_seconds=0.01)
        headers = {"Authorization": "Bearer " + token(actor.subject, "https://delivery.test")}
        yield server, headers, supervisor, client, gateway_setup
        client.close()


async def drive(supervisor, function, ref):
    task = asyncio.create_task(asyncio.to_thread(function, ref))
    async with asyncio.timeout(30):
        while not task.done():
            await asyncio.to_thread(supervisor.run_once)
            await asyncio.sleep(0.01)
        return await task


async def test_outbound_private_executor_completes_native_delivery_through_http(outbound_setup):
    server, headers, supervisor, client, gateway_setup = outbound_setup
    gateway, worker, pool, setup = gateway_setup
    service, actor, executor, cli, _, artifact, plan = setup
    response = server.post(
        "/v1/deployments",
        json=DeploymentSubmission(artifact_id=artifact.id, plan_id=plan.id).model_dump(mode="json"),
        headers={**headers, "Idempotency-Key": "outbound-deploy"},
    )
    assert response.status_code == 202
    ref = OperationRef(str(actor.tenant_id), response.json()["id"])
    assert await drive(supervisor, executor.execute, ref) == "submitted"
    assert await drive(supervisor, executor.observe, ref) == "terminal"
    operation = server.get(response.headers["Location"], headers=headers).json()
    assert operation["state"] == "succeeded" and operation["result"]["value"]["healthy"]
    assert len(cli.actions) == 2
    assert not hasattr(client, "database") and not hasattr(supervisor, "database")
    # Re-plan against the resulting owned app and update its existing native identity.
    planned = server.post(
        "/v1/plans",
        json={"artifact_id": str(artifact.id), "target_id": str(plan.target_id)},
        headers={**headers, "Idempotency-Key": "outbound-update-plan"},
    )
    await drive(
        supervisor, executor.execute, OperationRef(str(actor.tenant_id), planned.json()["id"])
    )
    next_plan = server.get(planned.headers["Location"], headers=headers).json()["result"]["value"]
    updated = server.post(
        "/v1/deployments",
        json={"artifact_id": str(artifact.id), "plan_id": next_plan["id"]},
        headers={**headers, "Idempotency-Key": "outbound-update"},
    )
    ref = OperationRef(str(actor.tenant_id), updated.json()["id"])
    await drive(supervisor, executor.execute, ref)
    await drive(supervisor, executor.observe, ref)
    second = server.get(updated.headers["Location"], headers=headers).json()
    assert second["state"] == "succeeded"
    assert (
        second["result"]["value"]["resources"][0]["native_id"]
        == operation["result"]["value"]["resources"][0]["native_id"]
    )
    assert len(cli.actions) == 4
    import sqlalchemy as sa
    from shifde_hosted.persistence import models as m

    with service.database.transaction(actor.tenant_id) as c:
        assert "native-only-local-test-secret" not in repr(
            c.execute(sa.select(m.execution_jobs)).all()
        )


async def test_source_build_has_no_native_credential_grant(outbound_setup):
    server, headers, supervisor, client, gateway_setup = outbound_setup
    _, _, _, setup = gateway_setup
    service, actor, executor, *_ = setup
    app = server.post(
        "/v1/applications",
        json={"name": "isolated-source", "kind": "databricks.app/v1"},
        headers=headers,
    ).json()
    scaffold = server.post(
        "/v1/scaffolds",
        json={"application_id": app["id"], "spec": {"kind": "databricks.app/v1"}},
        headers={**headers, "Idempotency-Key": "private-scaffold"},
    )
    original_lease = client.lease
    denied = []

    def check_lease():
        lease = original_lease()
        if lease:
            assert lease.job.credential_ref is None and lease.job.target is None
            with pytest.raises(RuntimeError, match="gateway rejected"):
                client.grant(lease)
            denied.append(lease.job.action)
        return lease

    client.lease = check_lease
    ref = OperationRef(str(actor.tenant_id), scaffold.json()["id"])
    await drive(supervisor, executor.execute, ref)
    source = server.get(scaffold.headers["Location"], headers=headers).json()["result"]["value"]
    project = server.post(
        "/v1/projects",
        json={
            "application_id": app["id"],
            "source_id": source["id"],
            "spec": {"kind": "databricks.app/v1"},
        },
        headers=headers,
    ).json()
    build = server.post(
        "/v1/builds",
        json={"project_id": project["id"]},
        headers={**headers, "Idempotency-Key": "private-build"},
    )
    await drive(
        supervisor, executor.execute, OperationRef(str(actor.tenant_id), build.json()["id"])
    )
    assert server.get(build.headers["Location"], headers=headers).json()["state"] == "succeeded"
    assert denied == ["scaffold", "build"]


async def test_uncertain_native_acceptance_is_not_replayed(outbound_setup):
    server, headers, supervisor, client, gateway_setup = outbound_setup
    _, _, _, setup = gateway_setup
    service, actor, executor, cli, _, artifact, plan = setup
    cli.fail_after_accept = True
    op = service.submit(
        actor,
        "deployment",
        DeploymentSubmission(artifact_id=artifact.id, plan_id=plan.id),
        "uncertain-private",
    )
    await drive(supervisor, executor.execute, OperationRef(str(actor.tenant_id), str(op.id)))
    assert service.get_operation(actor, op.id).state == "needs_attention"
    assert len(cli.actions) == 1
    assert not supervisor.run_once()


def test_worker_token_cannot_call_public_api_and_user_token_cannot_lease(outbound_setup):
    server, headers, supervisor, client, _ = outbound_setup
    assert (
        server.post(
            "/internal/v1/job-leases", json={"image": supervisor.settings.image}, headers=headers
        ).status_code
        == 401
    )
    assert (
        server.get(
            "/v1/operations/11111111-1111-4111-8111-111111111111",
            headers={"Authorization": "Bearer " + supervisor.settings.token_file.read_text()},
        ).status_code
        == 401
    )

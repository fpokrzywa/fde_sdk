import asyncio
from uuid import uuid4

import pytest
from shifde_contracts.hosted import ApplicationSubmission, ConnectionSubmission, TargetSubmission
from shifde_contracts.workflow import OperationRef
from shifde_executor.config import LocalCredentialBinding
from shifde_executor.runner import run_job
from shifde_hosted.config import CredentialBinding
from shifde_hosted.persistence import models as m
from shifde_snowflake.adapter import SnowflakeAdapter

from tests.adapters.snowflake_fake import FakeSnowflake
from tests.execution.test_private_worker import drive
from tests.execution.test_private_worker import outbound_setup as outbound_setup

pytestmark = pytest.mark.integration


@pytest.fixture
def snowflake_hosted(outbound_setup, tmp_path):
    server, headers, supervisor, client, setup = outbound_setup
    gateway, worker, pool, execution = setup
    service, actor, executor, *_ = execution
    host = f"https://{actor.tenant_id.hex}.snowflakecomputing.com"
    service.credential_bindings["snowflake"] = CredentialBinding(
        tenant_id=actor.tenant_id, host=host, mode="private", execution_pool_id=pool.id
    )
    connection = service.create_connection(
        actor, ConnectionSubmission(name="snowflake", host=host, credential_ref="snowflake")
    )
    target = service.create_target(
        actor,
        TargetSubmission(
            connection_id=connection.id,
            bindings={"package": "TEST_PACKAGE", "application": "TEST_APP", "warehouse": "TEST_WH"},
        ),
    )
    pool = pool.model_copy(
        update={
            "application_kinds": ["databricks.app/v1", "snowflake.native-app/v1"],
            "connection_ids": [*pool.connection_ids, connection.id],
        }
    )
    with service.database.transaction(actor.tenant_id) as c:
        c.execute(
            m.worker_pools.update()
            .where(m.worker_pools.c.id == pool.id)
            .values(data=pool.model_dump(mode="json"))
        )
    token_file = tmp_path / "snowflake-native-token"
    token_file.write_text("snowflake-local-only-secret")
    settings = supervisor.settings.model_copy(
        update={
            "credential_bindings": {
                **supervisor.settings.credential_bindings,
                "snowflake": LocalCredentialBinding(
                    tenant_id=actor.tenant_id,
                    host=host,
                    platform="snowflake",
                    mode="token_file",
                    token_file=token_file,
                    snowflake_account="TEST_ACCOUNT",
                    snowflake_role="DEPLOY_ROLE",
                ),
            }
        }
    )
    supervisor.settings, client.settings = settings, settings
    from shifde_executor.credentials import LocalCredentialBroker

    supervisor.broker = LocalCredentialBroker(settings)
    vendor = FakeSnowflake()

    class VendorBoundarySandbox:
        def run(self, job, handler, *, stop=None):
            class RPC:
                def call(self, method, data=None):
                    return handler(method, data or {})

            assert job.application_kind == "snowflake.native-app/v1"
            if job.target:
                auth = handler("credential", {})
                assert (
                    auth["bearer"] == "snowflake-local-only-secret"
                    and auth["snowflake"]["role"] == "DEPLOY_ROLE"
                )
            return {
                "status": "succeeded",
                "value": asyncio.run(
                    run_job(job, RPC(), adapter=SnowflakeAdapter(client_factory=lambda _: vendor))
                ),
            }

    supervisor.sandbox = VendorBoundarySandbox()
    return (
        server,
        headers,
        supervisor,
        gateway,
        worker,
        pool,
        service,
        actor,
        executor,
        vendor,
        target,
    )


SPEC = {
    "kind": "snowflake.native-app/v1",
    "package_binding": "package",
    "application_binding": "application",
    "warehouse_binding": "warehouse",
    "release_version": "V1",
    "release_patch": 0,
}


async def delivery_flow(setup, *, execute=None, timeout=30, promote=False):
    server, headers, supervisor, gateway, worker, pool, service, actor, executor, vendor, target = (
        setup
    )

    async def submit(route, body, key, deploy=False):
        response = server.post(
            "/v1/" + route, json=body, headers={**headers, "Idempotency-Key": key}
        )
        assert response.status_code == 202, response.text
        ref = OperationRef(str(actor.tenant_id), response.json()["id"])
        if execute:
            await execute(ref)
        else:
            await drive(supervisor, executor.execute, ref, timeout=timeout)
            if deploy and service.get_operation(actor, ref.operation_id).state == "running":
                await drive(supervisor, executor.observe, ref, timeout=timeout)
        result = server.get(response.headers["Location"], headers=headers).json()
        assert result["state"] == "succeeded", result
        return result["result"]["value"]

    app = server.post(
        "/v1/applications", json={"name": "snowflake-test", "kind": SPEC["kind"]}, headers=headers
    ).json()
    installed = None
    for index, (version, patch) in enumerate((("V1", 0), ("V1", 1), ("V2", 0))):
        spec = {**SPEC, "release_version": version, "release_patch": patch}
        source = await submit(
            "scaffolds", {"application_id": app["id"], "spec": spec}, f"scaffold-{index}"
        )
        project = server.post(
            "/v1/projects",
            json={"application_id": app["id"], "source_id": source["id"], "spec": spec},
            headers=headers,
        ).json()
        artifact = await submit("builds", {"project_id": project["id"]}, f"build-{index}")
        plan = await submit(
            "plans", {"artifact_id": artifact["id"], "target_id": str(target.id)}, f"plan-{index}"
        )
        deployed = await submit(
            "deployments",
            {"artifact_id": artifact["id"], "plan_id": plan["id"]},
            f"deploy-{index}",
            deploy=True,
        )
        native_id = next(r["native_id"] for r in deployed["resources"] if r["key"] == "application")
        assert deployed["healthy"] and (installed is None or native_id == installed)
        installed = native_id
    inspected = await submit(
        "inspections", {"application_id": app["id"], "target_id": str(target.id)}, "inspect"
    )
    assert any(r["key"] == "application" for r in inspected["resources"])
    if promote:
        response = server.post(
            "/v1/targets",
            headers=headers,
            json={
                "connection_id": str(target.connection_id),
                "bindings": {
                    "package": "PROMOTED_PACKAGE",
                    "application": "PROMOTED_APP",
                    "warehouse": "TEST_WH",
                },
            },
        )
        assert response.status_code == 201, response.text
        promoted = response.json()
        plan = await submit(
            "plans", {"artifact_id": artifact["id"], "target_id": promoted["id"]}, "promoted-plan"
        )
        deployed = await submit(
            "deployments",
            {"artifact_id": artifact["id"], "plan_id": plan["id"]},
            "promoted-deploy",
            deploy=True,
        )
        assert deployed["healthy"] and plan["artifact_id"] == artifact["id"]
        assert deployed["artifact_digest"] == artifact["archive"]["digest"]
        assert (
            next(r["native_id"] for r in deployed["resources"] if r["key"] == "application")
            != installed
        )
    return app, installed


async def test_hosted_snowflake_package_patch_version_and_in_place_upgrades(snowflake_hosted):
    await delivery_flow(snowflake_hosted, promote=True)
    vendor = snowflake_hosted[-2]
    assert [v["patch"] for v in vendor.versions["TEST_PACKAGE"] if v["version"] == "V1"] == [0, 1]
    assert len([m for m in vendor.mutations if m[0] == "install"]) == 2
    assert len([m for m in vendor.mutations if m[0] == "upgrade"]) == 2


async def test_failed_native_validation_exposes_partial_resources_and_no_success(snowflake_hosted):
    server, headers, supervisor, gateway, worker, pool, service, actor, executor, vendor, target = (
        snowflake_hosted
    )
    vendor.fail_validation = True
    app = service.create_application(
        actor,
        ApplicationSubmission(name="failed-validation", kind=SPEC["kind"]),
    )
    from shifde_contracts.hosted import ProjectSubmission, ScaffoldSubmission
    from shifde_contracts.submissions import BuildSubmission, DeploymentSubmission, PlanSubmission

    async def run(kind, body):
        op = service.submit(actor, kind, body, str(uuid4()))
        await drive(supervisor, executor.execute, OperationRef(str(actor.tenant_id), str(op.id)))
        return service.get_operation(actor, op.id)

    source = (
        await run("scaffold", ScaffoldSubmission(application_id=app.id, spec=SPEC))
    ).result.value
    project = service.create_project(
        actor, ProjectSubmission(application_id=app.id, source_id=source.id, spec=SPEC)
    )
    artifact = (await run("build", BuildSubmission(project_id=project.id))).result.value
    plan = (
        await run("plan", PlanSubmission(artifact_id=artifact.id, target_id=target.id))
    ).result.value
    operation = await run(
        "deployment", DeploymentSubmission(artifact_id=artifact.id, plan_id=plan.id)
    )
    assert operation.state == "needs_attention"
    view = server.get(f"/v1/deployments/{operation.id}", headers=headers).json()
    assert {r["key"] for r in view["deployment"]["resources"]} == {"package", "stage"}
    assert not view["deployment"]["healthy"] and not vendor.applications and not vendor.versions

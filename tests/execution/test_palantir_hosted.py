import asyncio
import json

import pytest
from shifde_contracts.hosted import ConnectionSubmission, TargetSubmission
from shifde_contracts.workflow import OperationRef
from shifde_executor.config import LocalCredentialBinding
from shifde_executor.credentials import LocalCredentialBroker
from shifde_executor.runner import run_job
from shifde_hosted.config import CredentialBinding
from shifde_hosted.persistence import models as m
from shifde_palantir.adapter import PalantirMarketplaceAdapter
from shifde_palantir.clients import PalantirClient

from tests.adapters.palantir_fake import FakePalantir, approval, make_bindings, make_spec
from tests.execution.test_private_worker import drive
from tests.execution.test_private_worker import outbound_setup as outbound_setup

pytestmark = pytest.mark.integration


@pytest.fixture
def palantir_hosted(outbound_setup, tmp_path, request):
    kind = getattr(request, "param", "palantir.marketplace/v1")
    server, headers, supervisor, client, setup = outbound_setup
    gateway, worker, pool, execution = setup
    service, actor, executor, *_ = execution
    host = f"https://{actor.tenant_id}.enrollment.example.test"
    service.credential_bindings["palantir"] = CredentialBinding(
        tenant_id=actor.tenant_id, host=host, mode="private", execution_pool_id=pool.id
    )
    connection = service.create_connection(
        actor, ConnectionSubmission(name="palantir", host=host, credential_ref="palantir")
    )
    target = service.create_target(
        actor,
        TargetSubmission(
            connection_id=connection.id,
            bindings=make_bindings(),
        ),
    )
    pool = pool.model_copy(
        update={
            "application_kinds": [
                "databricks.app/v1",
                "palantir.marketplace/v1",
                "palantir.superrepo/v1",
            ],
            "connection_ids": [*pool.connection_ids, connection.id],
        }
    )
    with service.database.transaction(actor.tenant_id) as c:
        c.execute(
            m.worker_pools.update()
            .where(m.worker_pools.c.id == pool.id)
            .values(data=pool.model_dump(mode="json"))
        )
    specs = [make_spec(), make_spec()]
    token_file = tmp_path / "palantir-token"
    token_file.write_text("palantir-only-local-secret")
    settings = supervisor.settings.model_copy(
        update={
            "heartbeat_seconds": 2,
            "state_dir": tmp_path / "receipts",
            "credential_bindings": {
                **supervisor.settings.credential_bindings,
                "palantir": LocalCredentialBinding(
                    tenant_id=actor.tenant_id,
                    host=host,
                    platform="palantir",
                    mode="token_file",
                    token_file=token_file,
                    allow_marketplace_preview=True,
                    palantir_approved_releases=[approval(spec, target.bindings) for spec in specs],
                ),
            },
        }
    )
    supervisor.settings, client.settings = settings, settings
    supervisor.broker = LocalCredentialBroker(settings)
    from shifde_executor.receipts import ReceiptOutbox

    supervisor.receipts = ReceiptOutbox(settings)
    vendor = FakePalantir(target.bindings)
    vendor.specs = specs

    class VendorBoundarySandbox:
        def run(self, job, handler, *, stop=None):
            class RPC:
                def call(self, method, data=None):
                    return handler(method, data or {})

            rpc = RPC()
            adapter = PalantirMarketplaceAdapter(
                client_factory=lambda target: PalantirClient(
                    target, lambda: rpc.call("credential"), transport=vendor.transport
                )
            )
            return {"status": "succeeded", "value": asyncio.run(run_job(job, rpc, adapter=adapter))}

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
        kind,
    )


async def palantir_flow(setup, *, execute=None, timeout=60):
    server, headers, supervisor, _, _, _, service, actor, executor, vendor, target, kind = setup

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
        op = server.get(response.headers["Location"], headers=headers).json()
        assert op["state"] == "succeeded", op
        assert "palantir-only-local-secret" not in json.dumps(op)
        return op["result"]["value"]

    app_response = server.post(
        "/v1/applications", json={"name": "palantir-test", "kind": kind}, headers=headers
    )
    assert app_response.status_code == 201, app_response.text
    app = app_response.json()
    first = None
    for index, spec in enumerate(vendor.specs):
        source = await submit(
            "scaffolds",
            {"application_id": app["id"], "spec": spec.model_dump(mode="json")},
            f"scaffold-{index}",
        )
        project = server.post(
            "/v1/projects",
            headers=headers,
            json={
                "application_id": app["id"],
                "spec": spec.model_dump(mode="json"),
                "source_id": source["id"],
            },
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
        assert deployed["healthy"]
        assert len(deployed["resources"]) == 1
        assert first is None or deployed["resources"] == first
        first = deployed["resources"]
    inspection = await submit(
        "inspections", {"application_id": app["id"], "target_id": str(target.id)}, "inspect"
    )
    assert inspection["resources"] == first
    assert len(vendor.installations) == 1 and len(vendor.jobs) == 2
    native = next(iter(vendor.installations.values()))
    assert native["latestTargetVersion"]["versionId"] == str(vendor.specs[-1].version_id)
    assert all("ontologyRid" not in (r[2] or {}) for r in vendor.requests if r[0] == "POST")


async def test_palantir_create_update_through_hosted_api_and_private_executor(palantir_hosted):
    await palantir_flow(palantir_hosted)

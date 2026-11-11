import asyncio
import json
from uuid import uuid4

import pytest
from shifde_contracts.hosted import ConnectionSubmission, TargetSubmission
from shifde_contracts.workflow import OperationRef
from shifde_executor.config import LocalCredentialBinding
from shifde_executor.credentials import LocalCredentialBroker
from shifde_executor.runner import run_job
from shifde_fabric.adapter import FabricAdapter, FabricIQAdapter
from shifde_fabric.clients import FabricClient
from shifde_hosted.config import CredentialBinding
from shifde_hosted.persistence import models as m

from tests.adapters.fabric_fake import FakeFabric
from tests.execution.test_private_worker import drive
from tests.execution.test_private_worker import outbound_setup as outbound_setup

pytestmark = pytest.mark.integration


@pytest.fixture
def fabric_hosted(outbound_setup, tmp_path, request):
    kind = getattr(request, "param", "fabric.solution/v1")
    server, headers, supervisor, client, setup = outbound_setup
    gateway, worker, pool, execution = setup
    service, actor, executor, *_ = execution
    host = "https://api.fabric.microsoft.com"
    service.credential_bindings["fabric"] = CredentialBinding(
        tenant_id=actor.tenant_id, host=host, mode="private", execution_pool_id=pool.id
    )
    connection = service.create_connection(
        actor, ConnectionSubmission(name="fabric", host=host, credential_ref="fabric")
    )
    target = service.create_target(
        actor,
        TargetSubmission(
            connection_id=connection.id,
            bindings={
                "workspace": str(uuid4()),
                "lake": "HostedLake",
                "notebook": "HostedNotebook",
                "ontology": "HostedOntology",
            },
        ),
    )
    pool = pool.model_copy(
        update={
            "application_kinds": ["databricks.app/v1", "fabric.solution/v1", "fabric.iq/v1"],
            "connection_ids": [*pool.connection_ids, connection.id],
        }
    )
    with service.database.transaction(actor.tenant_id) as c:
        c.execute(
            m.worker_pools.update()
            .where(m.worker_pools.c.id == pool.id)
            .values(data=pool.model_dump(mode="json"))
        )
    token_file = tmp_path / "fabric-token"
    token_file.write_text("fabric-only-local-secret")
    settings = supervisor.settings.model_copy(
        update={
            "heartbeat_seconds": 2,
            "state_dir": tmp_path / "receipts",
            "credential_bindings": {
                **supervisor.settings.credential_bindings,
                "fabric": LocalCredentialBinding(
                    tenant_id=actor.tenant_id,
                    host=host,
                    platform="fabric",
                    mode="token_file",
                    token_file=token_file,
                    allow_fabric_iq=True,
                    allow_notebook_execution=True,
                ),
            },
        }
    )
    supervisor.settings, client.settings = settings, settings
    supervisor.broker = LocalCredentialBroker(settings)
    from shifde_executor.receipts import ReceiptOutbox

    supervisor.receipts = ReceiptOutbox(settings)
    vendor = FakeFabric()

    class VendorBoundarySandbox:
        def run(self, job, handler, *, stop=None):
            class RPC:
                def call(self, method, data=None):
                    return handler(method, data or {})

            rpc = RPC()
            iq = job.application_kind == "fabric.iq/v1"
            adapter = (FabricIQAdapter if iq else FabricAdapter)(
                client_factory=lambda target: FabricClient(
                    target, lambda: rpc.call("credential"), transport=vendor.transport, iq=iq
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


async def fabric_flow(setup, *, execute=None, timeout=60, promote=False):
    (
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
    ) = setup

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
        assert "fabric-only-local-secret" not in json.dumps(op)
        return op["result"]["value"]

    app = server.post(
        "/v1/applications", json={"name": "fabric-test", "kind": kind}, headers=headers
    ).json()
    items = [
        dict(key="lake", item_type="Lakehouse", definition_path="lake.json"),
        dict(
            key="notebook",
            item_type="Notebook",
            definition_path="main.ipynb",
            depends_on=["lake"],
            lakehouse="lake",
        ),
    ]
    if kind == "fabric.iq/v1":
        items.append(
            dict(
                key="ontology",
                item_type="Ontology",
                definition_path="ontology",
                depends_on=["notebook"],
            )
        )
    spec = dict(kind=kind, items=items, verification_notebook="notebook")
    first = None
    for index in range(2):
        source = await submit(
            "scaffolds", {"application_id": app["id"], "spec": spec}, f"scaffold-{index}"
        )
        project = server.post(
            "/v1/projects",
            headers=headers,
            json={"application_id": app["id"], "spec": spec, "source_id": source["id"]},
        ).json()
        artifact = await submit("builds", {"project_id": project["id"]}, f"build-{index}")
        plan = await submit(
            "plans", {"artifact_id": artifact["id"], "target_id": str(target.id)}, f"plan-{index}"
        )
        result = await submit(
            "deployments",
            {"artifact_id": artifact["id"], "plan_id": plan["id"]},
            f"deploy-{index}",
            deploy=True,
        )
        resources = {resource["key"]: resource for resource in result["resources"]}
        assert len(resources) == len(result["resources"]) == len(items)
        assert result["healthy"] and (first is None or resources == first)
        first = resources
    inspection = await submit(
        "inspections", {"application_id": app["id"], "target_id": str(target.id)}, "inspect"
    )
    assert len(inspection["resources"]) == len(items)
    if promote:
        promoted = server.post(
            "/v1/targets",
            headers=headers,
            json={
                "connection_id": str(target.connection_id),
                "bindings": {**target.bindings, "workspace": str(uuid4())},
            },
        ).json()
        plan = await submit(
            "plans", {"artifact_id": artifact["id"], "target_id": promoted["id"]}, "promoted-plan"
        )
        result = await submit(
            "deployments",
            {"artifact_id": artifact["id"], "plan_id": plan["id"]},
            "promoted-deploy",
            deploy=True,
        )
        resources = {resource["key"]: resource for resource in result["resources"]}
        assert len(resources) == len(result["resources"]) == len(items)
        assert result["healthy"] and resources != first
    return artifact


@pytest.mark.parametrize("fabric_hosted", ["fabric.solution/v1", "fabric.iq/v1"], indirect=True)
async def test_fabric_create_update_and_promote_through_hosted_api(fabric_hosted):
    await fabric_flow(fabric_hosted, promote=True)
    assert len(fabric_hosted[-3].items) == (6 if fabric_hosted[-1] == "fabric.iq/v1" else 4)


@pytest.mark.parametrize("fabric_hosted", ["fabric.iq/v1"], indirect=True)
async def test_fabric_update_preserves_identity_with_reordered_owned_rows(
    fabric_hosted, monkeypatch
):
    executor = fabric_hosted[8]
    owned = executor._owned
    monkeypatch.setattr(
        executor,
        "_owned",
        lambda *args: tuple(sorted(owned(*args), key=lambda binding: binding.key, reverse=True)),
    )
    await fabric_flow(fabric_hosted)


async def test_fabric_lro_failure_keeps_partial_ownership_through_api(fabric_hosted):
    fabric_hosted[-3].fail_poll = True
    with pytest.raises(AssertionError):
        await fabric_flow(fabric_hosted)
    service, actor = fabric_hosted[6:8]
    with service.database.transaction(actor.tenant_id) as c:
        import sqlalchemy as sa

        resources = c.execute(sa.select(m.resource_bindings.c.key)).scalars().all()
        assert "lake" in resources and "notebook" not in resources

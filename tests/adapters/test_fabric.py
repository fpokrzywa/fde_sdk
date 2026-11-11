from uuid import uuid4

import pytest
from shifde_contracts.builds import BuildRequest, ScaffoldRequest
from shifde_contracts.projects import parse_spec
from shifde_core.artifact_store import LocalArtifactStore, read_archive
from shifde_core.contexts import BuildContext


@pytest.mark.parametrize("kind", ["fabric.solution/v1", "fabric.iq/v1"])
async def test_fabric_scaffold_and_build_never_execute_source(tmp_path, kind):
    from shifde_fabric.adapter import FabricAdapter, FabricIQAdapter
    from shifde_fabric.source import TOOLCHAIN_DIGEST

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
        items.append(dict(key="ontology", item_type="Ontology", definition_path="ontology"))
    spec = parse_spec(dict(kind=kind, items=items))
    adapter = FabricIQAdapter() if kind == "fabric.iq/v1" else FabricAdapter()
    ctx = BuildContext(
        tenant_id=uuid4(),
        artifacts=LocalArtifactStore(tmp_path),
        workspace=tmp_path / "build",
        toolchain_digest=TOOLCHAIN_DIGEST,
    )
    source = await adapter.scaffold(ScaffoldRequest(spec=spec), ctx)
    files = read_archive(ctx.artifacts.read(source, ctx.tenant_id))
    assert "main.ipynb" in files and "lake.json" in files
    artifact = await adapter.build(
        BuildRequest(application_id=uuid4(), spec=spec, source=source), ctx
    )
    assert artifact.archive.digest == source.digest


@pytest.fixture
async def prepared_fabric(tmp_path):
    from shifde_contracts.common import Target
    from shifde_contracts.plans import PlanRequest
    from shifde_core.contexts import DeployContext, ReadContext
    from shifde_core.journal import StepJournal
    from shifde_fabric.adapter import FabricIQAdapter
    from shifde_fabric.clients import FabricClient
    from shifde_fabric.source import TOOLCHAIN_DIGEST

    from tests.adapters.fabric_fake import FakeFabric

    tenant, operation = uuid4(), uuid4()
    target = Target(
        tenant_id=tenant,
        connection_id=uuid4(),
        host="https://api.fabric.microsoft.com",
        bindings={
            "workspace": str(uuid4()),
            "lake": "DemoLake",
            "notebook": "DemoNotebook",
            "ontology": "DemoOntology",
        },
    )
    vendor = FakeFabric()
    auth = {
        "bearer": "test-fabric-local-secret",
        "fabric": {"host": target.host, "allow_iq": True, "allow_notebook_execution": True},
    }
    adapter = FabricIQAdapter(
        client_factory=lambda t: FabricClient(t, lambda: auth, transport=vendor.transport, iq=True)
    )
    spec = parse_spec(
        dict(
            kind="fabric.iq/v1",
            items=[
                dict(key="lake", item_type="Lakehouse", definition_path="lake.json"),
                dict(
                    key="notebook",
                    item_type="Notebook",
                    definition_path="main.ipynb",
                    depends_on=["lake"],
                    lakehouse="lake",
                ),
                dict(
                    key="ontology",
                    item_type="Ontology",
                    definition_path="ontology",
                    depends_on=["notebook"],
                ),
            ],
        )
    )
    build = BuildContext(
        tenant_id=tenant,
        artifacts=LocalArtifactStore(tmp_path),
        workspace=tmp_path / "build",
        toolchain_digest=TOOLCHAIN_DIGEST,
    )
    source = await adapter.scaffold(ScaffoldRequest(spec=spec), build)
    artifact = await adapter.build(
        BuildRequest(application_id=uuid4(), spec=spec, source=source), build
    )
    read = ReadContext(tenant_id=tenant, target=target, operation_id=operation)
    plan = await adapter.plan(PlanRequest(artifact=artifact), read)
    deploy = DeployContext(
        tenant_id=tenant,
        target=target,
        operation_id=operation,
        artifacts=build.artifacts,
        journal=StepJournal(tmp_path / "journal.json", tenant, operation),
        toolchain_digest=TOOLCHAIN_DIGEST,
    )
    return adapter, vendor, auth, artifact, plan, deploy, build, read


async def test_create_201_and_202_preserves_dependency_order_and_rebinds_notebook(prepared_fabric):
    import base64
    import json

    from shifde_contracts.execution import ApplyRequest, VerifyRequest

    adapter, vendor, _, artifact, plan, ctx, *_ = prepared_fabric
    receipt = await adapter.apply(ApplyRequest(artifact=artifact, plan=plan), ctx)
    creates = [
        r
        for r in vendor.requests
        if r[0] == "POST" and r[1].split("/")[-1] in {"lakehouses", "notebooks", "ontologies"}
    ]
    assert [r[1].split("/")[-1] for r in creates] == ["lakehouses", "notebooks", "ontologies"]
    notebook = json.loads(base64.b64decode(creates[1][2]["definition"]["parts"][0]["payload"]))
    lake_id = next(r.native_id.split(":")[-1] for r in receipt.resources if r.key == "lake")
    assert notebook["metadata"]["dependencies"]["lakehouse"]["default_lakehouse"] == lake_id
    assert (await adapter.verify(VerifyRequest(receipt=receipt, spec=artifact.spec), ctx)).passed
    assert ctx.journal.completed("notebook-metadata-submit")["handles"]["operation_id"]
    assert not any("untrusted" in path for _, path, _ in vendor.requests)


async def upgraded(prepared, receipt, *, execute=False):
    import json
    from dataclasses import replace

    from shifde_contracts.plans import PlanRequest
    from shifde_core.artifact_store import create_archive
    from shifde_core.journal import StepJournal

    adapter, _, _, artifact, _, ctx, build, read = prepared
    spec = artifact.spec.model_copy(
        update={"verification_notebook": "notebook" if execute else None}
    )
    files = read_archive(build.artifacts.read(artifact.archive, ctx.tenant_id))
    notebook = json.loads(files["main.ipynb"])
    notebook["cells"][0]["source"] = ["assert spark.range(2).count() == 2\n"]
    files["main.ipynb"] = json.dumps(notebook).encode()
    source = build.artifacts.put(ctx.tenant_id, create_archive(files))
    artifact = await adapter.build(
        BuildRequest(application_id=artifact.application_id, spec=spec, source=source), build
    )
    operation = uuid4()
    read = replace(read, owned_resources=tuple(receipt.resources), operation_id=operation)
    plan = await adapter.plan(PlanRequest(artifact=artifact), read)
    ctx = replace(
        ctx,
        owned_resources=read.owned_resources,
        operation_id=operation,
        journal=StepJournal(
            build.artifacts.root / (str(operation) + ".json"), ctx.tenant_id, operation
        ),
    )
    return artifact, plan, ctx


async def test_update_retains_native_ids_and_does_not_duplicate_lakehouse(prepared_fabric):
    from shifde_contracts.execution import ApplyRequest, VerifyRequest

    adapter, vendor, _, artifact, plan, ctx, *_ = prepared_fabric
    first = await adapter.apply(ApplyRequest(artifact=artifact, plan=plan), ctx)
    artifact, plan, ctx = await upgraded(prepared_fabric, first)
    second = await adapter.apply(ApplyRequest(artifact=artifact, plan=plan), ctx)
    assert first.resources == second.resources and len(vendor.items) == 3
    assert (await adapter.verify(VerifyRequest(receipt=second, spec=artifact.spec), ctx)).passed


async def test_lost_acceptance_blocks_blind_create_retry(prepared_fabric):
    import httpx
    from shifde_contracts.execution import ApplyRequest
    from shifde_core.journal import NeedsAttention

    adapter, vendor, _, artifact, plan, ctx, *_ = prepared_fabric
    vendor.lose_response = True
    with pytest.raises(httpx.ReadTimeout):
        await adapter.apply(ApplyRequest(artifact=artifact, plan=plan), ctx)
    with pytest.raises(NeedsAttention):
        await adapter.apply(ApplyRequest(artifact=artifact, plan=plan), ctx)
    assert len(vendor.items) == 1


async def test_accepted_lro_is_durable_before_poll_failure(prepared_fabric):
    from shifde_contracts.execution import ApplyRequest
    from shifde_fabric.clients import FabricAPIError

    adapter, vendor, _, artifact, plan, ctx, *_ = prepared_fabric
    vendor.fail_poll = True
    with pytest.raises(FabricAPIError):
        await adapter.apply(ApplyRequest(artifact=artifact, plan=plan), ctx)
    assert ctx.journal.completed("lake-metadata-complete")["resources"]
    assert ctx.journal.completed("notebook-metadata-submit")["handles"]["operation_id"]
    assert ctx.journal.completed("notebook-metadata-complete") is None


async def test_required_execution_cannot_pass_on_metadata_alone(prepared_fabric):
    from shifde_contracts.execution import ApplyRequest, VerifyRequest

    adapter, vendor, auth, artifact, plan, ctx, *_ = prepared_fabric
    first = await adapter.apply(ApplyRequest(artifact=artifact, plan=plan), ctx)
    artifact, plan, ctx = await upgraded(prepared_fabric, first, execute=True)
    vendor.job_status = "InProgress"
    receipt = await adapter.apply(ApplyRequest(artifact=artifact, plan=plan), ctx)
    assert not (
        await adapter.verify(VerifyRequest(receipt=receipt, spec=artifact.spec), ctx)
    ).passed
    vendor.jobs[receipt.operation_handles["job_id"]]["status"] = "Completed"
    assert (await adapter.verify(VerifyRequest(receipt=receipt, spec=artifact.spec), ctx)).passed
    artifact, plan, ctx = await upgraded(prepared_fabric, receipt, execute=True)
    auth["fabric"]["allow_notebook_execution"] = False
    with pytest.raises(PermissionError):
        await adapter.apply(ApplyRequest(artifact=artifact, plan=plan), ctx)


async def test_missing_capacity_blocks_before_any_native_mutation(prepared_fabric):
    from shifde_contracts.plans import PlanRequest

    adapter, vendor, _, artifact, _, _, _, read = prepared_fabric
    vendor.capacity = False
    with pytest.raises(ValueError, match="capacity"):
        await adapter.plan(PlanRequest(artifact=artifact), read)
    assert not vendor.items


async def test_notebook_parameter_metadata_drift_is_not_reported_healthy(prepared_fabric):
    import base64
    import json

    from shifde_contracts.execution import ApplyRequest, VerifyRequest

    adapter, vendor, _, artifact, plan, ctx, *_ = prepared_fabric
    receipt = await adapter.apply(ApplyRequest(artifact=artifact, plan=plan), ctx)
    notebook = next(i for i in vendor.items.values() if i["type"] == "Notebook")
    part = notebook["definition"]["parts"][0]
    doc = json.loads(base64.b64decode(part["payload"]))
    doc["cells"][0]["metadata"] = {"tags": ["parameters"]}
    part["payload"] = base64.b64encode(json.dumps(doc).encode()).decode()
    assert not (
        await adapter.verify(VerifyRequest(receipt=receipt, spec=artifact.spec), ctx)
    ).passed


async def test_iq_adapter_enforces_feature_gate_even_when_client_factory_omits_iq(prepared_fabric):
    from shifde_contracts.plans import PlanRequest
    from shifde_fabric.clients import FabricClient

    adapter, vendor, auth, artifact, _, _, _, read = prepared_fabric
    auth["fabric"]["allow_iq"] = False
    adapter.client_factory = lambda target: FabricClient(
        target, lambda: auth, transport=vendor.transport
    )
    with pytest.raises(PermissionError):
        await adapter.plan(PlanRequest(artifact=artifact), read)


async def test_iq_tmdl_uses_explicit_environment_bindings(prepared_fabric):
    import base64

    from shifde_contracts.execution import ApplyRequest
    from shifde_contracts.plans import PlanRequest
    from shifde_core.artifact_store import create_archive

    adapter, vendor, _, artifact, _, ctx, build, read = prepared_fabric
    files = read_archive(build.artifacts.read(artifact.archive, ctx.tenant_id))
    files["ontology/model.tmdl"] += b"\n// {{fabric.workspace.id}} {{fabric.item.notebook.id}}\n"
    source = build.artifacts.put(ctx.tenant_id, create_archive(files))
    artifact = await adapter.build(
        BuildRequest(application_id=artifact.application_id, spec=artifact.spec, source=source),
        build,
    )
    plan = await adapter.plan(PlanRequest(artifact=artifact), read)
    receipt = await adapter.apply(ApplyRequest(artifact=artifact, plan=plan), ctx)
    ontology = next(i for i in vendor.items.values() if i["type"] == "Ontology")
    text = base64.b64decode(
        next(p["payload"] for p in ontology["definition"]["parts"] if p["path"] == "model.tmdl")
    ).decode()
    notebook_id = next(r.native_id.split(":")[-1] for r in receipt.resources if r.key == "notebook")
    assert (
        "{{fabric" not in text and notebook_id in text and ctx.target.bindings["workspace"] in text
    )

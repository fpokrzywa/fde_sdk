import base64
import json
from dataclasses import replace
from uuid import uuid4

import pytest
from shifde_contracts.builds import BuildRequest
from shifde_contracts.execution import ApplyRequest, VerifyRequest
from shifde_contracts.plans import PlanRequest
from shifde_contracts.projects import parse_spec
from shifde_core.artifact_store import create_archive, read_archive
from shifde_core.journal import StepJournal
from shifde_fabric.clients import FabricAPIError

from tests.adapters.test_fabric import prepared_fabric as prepared_fabric
from tests.adapters.test_fabric import upgraded


async def rebuild(prepared, *, edit=None, spec=None, target=None):
    adapter, _, _, artifact, _, ctx, build, read = prepared
    spec = spec or artifact.spec
    files = read_archive(build.artifacts.read(artifact.archive, ctx.tenant_id))
    if edit:
        edit(files)
    source = build.artifacts.put(ctx.tenant_id, create_archive(files))
    artifact = await adapter.build(
        BuildRequest(application_id=artifact.application_id, spec=spec, source=source), build
    )
    if target:
        ctx, read = replace(ctx, target=target), replace(read, target=target)
    plan = await adapter.plan(PlanRequest(artifact=artifact), read)
    return artifact, plan, ctx


async def test_successful_definition_updates_do_not_require_lro_results(prepared_fabric):
    adapter, vendor, _, artifact, plan, ctx, *_ = prepared_fabric
    first = await adapter.apply(ApplyRequest(artifact=artifact, plan=plan), ctx)
    vendor.no_update_result = True
    artifact, plan, ctx = await upgraded(prepared_fabric, first)
    await adapter.apply(ApplyRequest(artifact=artifact, plan=plan), ctx)


async def test_non_schema_lakehouse_omits_creation_payload(prepared_fabric):
    def edit(files):
        files["lake.json"] = b'{"enableSchemas":false}'

    artifact, plan, ctx = await rebuild(prepared_fabric, edit=edit)
    await prepared_fabric[0].apply(ApplyRequest(artifact=artifact, plan=plan), ctx)
    lake = next(
        r for r in prepared_fabric[1].requests if r[0] == "POST" and r[1].endswith("/lakehouses")
    )
    assert "creationPayload" not in lake[2]


@pytest.mark.parametrize(
    "text",
    [
        "metric Revenue\n\ttype: explicit\n",
        "relationship Sales\n\ttype: singleColumn\n",
        "rule Check\n\truleReferencedEntity Item\n\t\tpropertyScope: none\n",
        "entity Item\n\tresourceLink Dashboard\n\t\titem\n\t\t\ttype: Direct\n",
    ],
)
async def test_service_elided_tmdl_defaults_fail_source_validation_before_writes(
    prepared_fabric, text
):
    def edit(files):
        files["ontology/model.tmdl"] += text.encode()

    with pytest.raises(ValueError):
        await rebuild(prepared_fabric, edit=edit)
    assert not prepared_fabric[1].items


@pytest.mark.parametrize(
    "drift", ["environment", "kernelspec", "lake-description", "ontology-property"]
)
async def test_managed_runtime_and_metadata_drift_cannot_pass_verification(prepared_fabric, drift):
    adapter, vendor, _, artifact, plan, ctx, *_ = prepared_fabric
    receipt = await adapter.apply(ApplyRequest(artifact=artifact, plan=plan), ctx)
    if drift == "lake-description":
        next(i for i in vendor.items.values() if i["type"] == "Lakehouse")["description"] = (
            "edited by external writer"
        )
    else:
        item = next(
            i
            for i in vendor.items.values()
            if i["type"] == ("Ontology" if drift == "ontology-property" else "Notebook")
        )
        part = item["definition"]["parts"][0]
        data = base64.b64decode(part["payload"])
        if drift == "ontology-property":
            data += b"\nannotation changed = true\n"
        else:
            doc = json.loads(data)
            if drift == "environment":
                doc["metadata"]["dependencies"]["environment"] = {"environmentId": str(uuid4())}
            else:
                doc["metadata"]["kernelspec"] = {"name": "other-runtime"}
            data = json.dumps(doc).encode()
        part["payload"] = base64.b64encode(data).decode()
    assert not (
        await adapter.verify(VerifyRequest(receipt=receipt, spec=artifact.spec), ctx)
    ).passed


async def test_known_execution_denial_is_rejected_in_plan_and_before_apply_intent(prepared_fabric):
    adapter, vendor, auth, artifact, _, ctx, build, read = prepared_fabric
    spec = artifact.spec.model_copy(update={"verification_notebook": "notebook"})
    source = await adapter.scaffold(
        __import__("shifde_contracts.builds", fromlist=["ScaffoldRequest"]).ScaffoldRequest(
            spec=spec
        ),
        build,
    )
    artifact = await adapter.build(
        BuildRequest(application_id=artifact.application_id, spec=spec, source=source), build
    )
    auth["fabric"]["allow_notebook_execution"] = False
    with pytest.raises(PermissionError):
        await adapter.plan(PlanRequest(artifact=artifact), read)
    auth["fabric"]["allow_notebook_execution"] = True
    plan = await adapter.plan(PlanRequest(artifact=artifact), read)
    auth["fabric"]["allow_notebook_execution"] = False
    with pytest.raises(PermissionError):
        await adapter.apply(ApplyRequest(artifact=artifact, plan=plan), ctx)
    assert not vendor.items and not any(ctx.journal.has_intent(s.key) for s in plan.steps)


@pytest.mark.parametrize(
    "corrupt", ["empty-resources", "missing-definition", "missing-verification"]
)
async def test_incomplete_receipts_are_rejected_before_any_healthy_result(prepared_fabric, corrupt):
    adapter, _, _, artifact, plan, ctx, *_ = prepared_fabric
    first = await adapter.apply(ApplyRequest(artifact=artifact, plan=plan), ctx)
    artifact, plan, ctx = await upgraded(prepared_fabric, first, execute=True)
    receipt = await adapter.apply(ApplyRequest(artifact=artifact, plan=plan), ctx)
    handles = dict(receipt.operation_handles)
    if corrupt == "missing-definition":
        handles.pop("notebook:definition")
    if corrupt == "missing-verification":
        handles.pop("verification")
    receipt = receipt.model_copy(
        update={
            "operation_handles": handles,
            "resources": [] if corrupt == "empty-resources" else receipt.resources,
        }
    )
    with pytest.raises(ValueError):
        await adapter.verify(VerifyRequest(receipt=receipt, spec=artifact.spec), ctx)


async def test_maximum_length_local_key_supports_create_and_update_checkpoints(prepared_fabric):
    adapter, _, _, artifact, _, ctx, build, read = prepared_fabric
    long = "l" * 64
    doc = artifact.spec.model_dump()
    doc["items"][0]["key"] = long
    doc["items"][1].update(depends_on=[long], lakehouse=long)
    spec = parse_spec(doc)
    target = ctx.target.model_copy(
        update={"bindings": {**ctx.target.bindings, long: "LongKeyLake"}}
    )
    artifact, plan, ctx = await rebuild(prepared_fabric, spec=spec, target=target)
    receipt = await adapter.apply(ApplyRequest(artifact=artifact, plan=plan), ctx)
    operation = uuid4()
    read = replace(
        read, target=target, owned_resources=tuple(receipt.resources), operation_id=operation
    )
    plan = await adapter.plan(PlanRequest(artifact=artifact), read)
    ctx = replace(
        ctx,
        owned_resources=read.owned_resources,
        operation_id=operation,
        journal=StepJournal(build.artifacts.root / "long-update.json", ctx.tenant_id, operation),
    )
    result = await adapter.apply(ApplyRequest(artifact=artifact, plan=plan), ctx)
    assert result.resources == receipt.resources and all(len(s.key) <= 64 for s in plan.steps)


@pytest.mark.parametrize(
    "invalid", ["null-dependencies", "unterminated-token", "invalid-ontology-name"]
)
async def test_deterministic_source_and_name_failures_precede_native_writes(
    prepared_fabric, invalid
):
    def edit(files):
        if invalid == "null-dependencies":
            doc = json.loads(files["main.ipynb"])
            doc["metadata"]["dependencies"] = None
            files["main.ipynb"] = json.dumps(doc).encode()
        elif invalid == "unterminated-token":
            files["ontology/model.tmdl"] += b"\n// {{fabric.item.lake.id\n"

    target = prepared_fabric[5].target
    if invalid == "invalid-ontology-name":
        target = target.model_copy(
            update={"bindings": {**target.bindings, "ontology": "Sales Ontology"}}
        )
    with pytest.raises(ValueError):
        await rebuild(prepared_fabric, edit=edit, target=target)
    assert not prepared_fabric[1].items


async def test_async_create_identity_persisted_before_followup_get(prepared_fabric):
    adapter, vendor, _, artifact, plan, ctx, *_ = prepared_fabric
    vendor.lro_types.add("Lakehouse")
    vendor.fail_item_get = True
    with pytest.raises(FabricAPIError):
        await adapter.apply(ApplyRequest(artifact=artifact, plan=plan), ctx)
    completed = ctx.journal.completed("lake-metadata-complete")
    assert completed is not None and completed["resources"][0]["key"] == "lake"


async def test_wire_receipt_budget_checked_before_any_native_writes(prepared_fabric):
    from shifde_contracts.builds import ScaffoldRequest

    adapter, vendor, _, artifact, _, ctx, build, read = prepared_fabric
    spec = parse_spec(
        {
            "kind": "fabric.iq/v1",
            "items": [
                {
                    "key": "lake" + str(n),
                    "item_type": "Lakehouse",
                    "definition_path": "lake" + str(n) + ".json",
                }
                for n in range(15)
            ]
            + [{"key": "ontology", "item_type": "Ontology", "definition_path": "ontology"}],
        }
    )
    target = ctx.target.model_copy(
        update={
            "bindings": {
                **ctx.target.bindings,
                **{"lake" + str(n): "Lake" + str(n) for n in range(15)},
            }
        }
    )
    source = await adapter.scaffold(ScaffoldRequest(spec=spec), build)
    files = read_archive(build.artifacts.read(source, ctx.tenant_id))
    for name in files:
        if name.endswith(".json"):
            files[name] = json.dumps({"description": "界" * 256}).encode()
    source = build.artifacts.put(ctx.tenant_id, create_archive(files))
    artifact = await adapter.build(
        BuildRequest(application_id=artifact.application_id, spec=spec, source=source), build
    )
    plan = await adapter.plan(PlanRequest(artifact=artifact), replace(read, target=target))
    with pytest.raises(ValueError, match="receipt"):
        await adapter.apply(ApplyRequest(artifact=artifact, plan=plan), replace(ctx, target=target))
    assert not vendor.items

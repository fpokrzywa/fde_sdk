from uuid import uuid4

import pytest
from shifde_contracts.builds import BuildRequest, ScaffoldRequest
from shifde_contracts.projects import parse_spec
from shifde_core.artifact_store import LocalArtifactStore
from shifde_core.contexts import BuildContext


async def test_palantir_marketplace_build_is_offline_and_pins_version(tmp_path):
    from shifde_palantir.adapter import PalantirMarketplaceAdapter
    from shifde_palantir.source import TOOLCHAIN_DIGEST

    spec = parse_spec(
        {
            "kind": "palantir.marketplace/v1",
            "version_id": str(uuid4()),
            "object_type": "Example",
            "frontend_asset_path": "health.txt",
            "frontend_asset_sha256": "a" * 64,
        }
    )
    ctx = BuildContext(
        tenant_id=uuid4(),
        artifacts=LocalArtifactStore(tmp_path),
        workspace=tmp_path / "build",
        toolchain_digest=TOOLCHAIN_DIGEST,
    )
    adapter = PalantirMarketplaceAdapter()
    source = await adapter.scaffold(ScaffoldRequest(spec=spec), ctx)
    artifact = await adapter.build(
        BuildRequest(application_id=uuid4(), spec=spec, source=source), ctx
    )
    assert artifact.archive.digest == source.digest and artifact.spec.version_id == spec.version_id


@pytest.mark.parametrize(
    "operation",
    ["scaffold", "validate", "build", "inspect", "plan", "apply", "observe", "verify", "reconcile"],
)
async def test_superrepo_stays_explicitly_gated(operation):
    from shifde_contracts.platform_specs.palantir import FoundryApplicationSpec
    from shifde_palantir.adapter import PalantirSuperRepoAdapter, SuperRepoPrerequisite

    with pytest.raises(SuperRepoPrerequisite, match="enrollment"):
        await getattr(PalantirSuperRepoAdapter(), operation)(
            ScaffoldRequest(
                spec=FoundryApplicationSpec(
                    ontology_binding="ontology",
                    osdk_package="@ontology/sdk",
                    product_binding="product",
                )
            ),
            None,
        )


async def test_superrepo_capabilities_report_enrollment_and_cli_prerequisites():
    from shifde_palantir.adapter import PalantirSuperRepoAdapter

    report = await PalantirSuperRepoAdapter().capabilities(None)
    deployment = next(c for c in report.capabilities if c.operation == "deployment")
    assert deployment.status == "unsupported"
    assert "enrollment" in deployment.reason and "CLI" in deployment.reason


@pytest.fixture
async def prepared_palantir(tmp_path):
    from shifde_contracts.common import Target
    from shifde_contracts.plans import PlanRequest
    from shifde_core.contexts import DeployContext, ReadContext
    from shifde_core.journal import StepJournal
    from shifde_palantir.adapter import PalantirMarketplaceAdapter
    from shifde_palantir.clients import PalantirClient
    from shifde_palantir.source import TOOLCHAIN_DIGEST

    from tests.adapters.palantir_fake import FakePalantir, approval, make_bindings, make_spec

    tenant, operation = uuid4(), uuid4()
    target = Target(
        tenant_id=tenant,
        connection_id=uuid4(),
        host="https://enrollment.example.test",
        bindings=make_bindings(),
    )
    spec = make_spec()
    vendor = FakePalantir(target.bindings)
    auth = {
        "bearer": "palantir-only-local-secret",
        "palantir": {
            "host": target.host,
            "allow_marketplace_preview": True,
            "approved_releases": [approval(spec, target.bindings)],
        },
    }
    adapter = PalantirMarketplaceAdapter(
        client_factory=lambda t: PalantirClient(t, lambda: auth, transport=vendor.transport)
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
    return adapter, vendor, auth, artifact, plan, deploy, read


async def test_registration_job_and_frontend_are_separate_proofs(prepared_palantir):
    from shifde_contracts.execution import ApplyRequest, VerifyRequest

    adapter, vendor, _, artifact, plan, ctx, read = prepared_palantir
    receipt = await adapter.apply(ApplyRequest(artifact=artifact, plan=plan), ctx)
    assert ctx.journal.completed("palantir-installation-create")
    assert (
        ctx.journal.completed("palantir-job-submit")["handles"]["job_id"]
        == receipt.operation_handles["job_id"]
    )
    request = VerifyRequest(receipt=receipt, spec=artifact.spec)
    assert (await adapter.verify(request, read)).passed
    vendor.frontend = b"<html>SSO login</html>"
    assert not (await adapter.verify(request, read)).passed
    assert all("authorization" not in r[3] for r in vendor.requests if r[1] == "/assets/health.txt")


@pytest.mark.parametrize(
    "status,state",
    [
        ("PREPARING", "running"),
        ("BUILDING", "running"),
        ("FAILED", "unknown"),
        ("ABORTED", "unknown"),
        ("IDLE_DRAFT", "unknown"),
        ("invented", "unknown"),
    ],
)
async def test_jobs_with_partial_or_unknown_effects_keep_claims(prepared_palantir, status, state):
    from shifde_contracts.execution import ApplyRequest, ObserveRequest

    adapter, vendor, _, artifact, plan, ctx, read = prepared_palantir
    vendor.status = status
    receipt = await adapter.apply(ApplyRequest(artifact=artifact, plan=plan), ctx)
    assert (
        await adapter.observe(ObserveRequest(receipt=receipt, spec=artifact.spec), read)
    ).state == state


@pytest.mark.parametrize("fault", ["lose_registration", "lose_job", "fail_readback"])
async def test_uncertain_writes_never_replay_and_keep_known_ids(prepared_palantir, fault):
    from shifde_contracts.execution import ApplyRequest
    from shifde_core.journal import NeedsAttention

    adapter, vendor, _, artifact, plan, ctx, _ = prepared_palantir
    setattr(vendor, fault, True)
    request = ApplyRequest(artifact=artifact, plan=plan)
    import httpx

    with pytest.raises((httpx.ReadError, RuntimeError)):
        await adapter.apply(request, ctx)
    if fault != "lose_registration":
        assert ctx.journal.completed("palantir-installation-create")["resources"]
    count = sum(r[0] == "POST" for r in vendor.requests)
    with pytest.raises(NeedsAttention, match="replay"):
        await adapter.apply(request, ctx)
    assert sum(r[0] == "POST" for r in vendor.requests) == count


async def test_schema_drift_and_unapproved_versions_fail_before_mutation(prepared_palantir):
    from shifde_contracts.execution import ApplyRequest

    adapter, vendor, auth, artifact, plan, ctx, _ = prepared_palantir
    vendor.schema_revision += 1
    with pytest.raises(ValueError, match="changed"):
        await adapter.apply(ApplyRequest(artifact=artifact, plan=plan), ctx)
    auth["palantir"]["approved_releases"] = []
    with pytest.raises(PermissionError, match="approval"):
        await adapter.apply(ApplyRequest(artifact=artifact, plan=plan), ctx)
    assert not any(r[0] == "POST" for r in vendor.requests)


async def test_native_ontology_attachment_cannot_be_accepted_by_a_new_update_plan(
    prepared_palantir,
):
    from dataclasses import replace

    from shifde_contracts.execution import ApplyRequest
    from shifde_contracts.plans import PlanRequest
    from shifde_core.journal import NeedsAttention

    adapter, vendor, _, artifact, plan, ctx, read = prepared_palantir
    receipt = await adapter.apply(ApplyRequest(artifact=artifact, plan=plan), ctx)
    native = next(iter(vendor.installations.values()))
    native["ontologyRid"] = read.target.bindings["ontology"]
    read = replace(read, owned_resources=tuple(receipt.resources))
    count = len(vendor.jobs)
    with pytest.raises(NeedsAttention, match="Ontology"):
        await adapter.plan(PlanRequest(artifact=artifact), read)
    assert len(vendor.jobs) == count

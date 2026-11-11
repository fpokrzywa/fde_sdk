from uuid import uuid4

import pytest
from shifde_contracts.builds import BuildRequest, ScaffoldRequest
from shifde_contracts.common import Target
from shifde_contracts.platform_specs.snowflake import SnowflakeApplicationSpec
from shifde_core.artifact_store import LocalArtifactStore, read_archive
from shifde_core.contexts import BuildContext


async def test_snowflake_scaffold_build_is_offline_and_rejects_local_hooks(tmp_path):
    from shifde_core.artifact_store import create_archive
    from shifde_snowflake.adapter import SnowflakeAdapter
    from shifde_snowflake.source import TOOLCHAIN_DIGEST

    tenant = uuid4()
    ctx = BuildContext(
        tenant_id=tenant,
        artifacts=LocalArtifactStore(tmp_path),
        toolchain_digest=TOOLCHAIN_DIGEST,
        workspace=tmp_path / "build",
    )
    spec = SnowflakeApplicationSpec(
        package_binding="package",
        application_binding="application",
        warehouse_binding="warehouse",
        release_version="V1",
    )
    adapter = SnowflakeAdapter()
    source = await adapter.scaffold(ScaffoldRequest(spec=spec), ctx)
    files = read_archive(ctx.artifacts.read(source, tenant))
    assert {
        "manifest.yml",
        "setup.sql",
        "ui/streamlit_app.py",
        "ui/environment.yml",
    } <= files.keys()
    artifact = await adapter.build(
        BuildRequest(application_id=uuid4(), source=source, spec=spec), ctx
    )
    assert artifact.archive.digest == source.digest
    injected = ctx.artifacts.put(
        tenant,
        create_archive({**files, "snowflake.yml": b"native_app:\n  post_deploy: [rm -rf /]\n"}),
    )
    with pytest.raises(ValueError, match="source"):
        await adapter.build(
            BuildRequest(application_id=artifact.application_id, source=injected, spec=spec), ctx
        )


@pytest.mark.parametrize(
    "name", ["APP; DROP DATABASE X", 'APP"X', "DB.SCHEMA", "name with space", "../APP"]
)
def test_native_identifiers_reject_injection_and_ambiguous_names(name):
    from shifde_snowflake.source import identifier

    with pytest.raises(ValueError):
        identifier(name)


@pytest.fixture
async def prepared_snowflake(tmp_path):
    from shifde_contracts.plans import PlanRequest
    from shifde_core.contexts import DeployContext, ReadContext
    from shifde_core.journal import StepJournal
    from shifde_snowflake.adapter import SnowflakeAdapter
    from shifde_snowflake.source import TOOLCHAIN_DIGEST

    from tests.adapters.snowflake_fake import FakeSnowflake

    tenant, app = uuid4(), uuid4()
    store = LocalArtifactStore(tmp_path / "artifacts")
    build = BuildContext(
        tenant_id=tenant,
        artifacts=store,
        toolchain_digest=TOOLCHAIN_DIGEST,
        workspace=tmp_path / "build",
    )
    target = Target(
        tenant_id=tenant,
        connection_id=uuid4(),
        host="https://test.snowflakecomputing.com",
        bindings={"package": "TEST_PACKAGE", "application": "TEST_APP", "warehouse": "TEST_WH"},
    )
    read = ReadContext(tenant_id=tenant, target=target)
    vendor = FakeSnowflake()
    adapter = SnowflakeAdapter(client_factory=lambda _: vendor)
    spec = SnowflakeApplicationSpec(
        package_binding="package",
        application_binding="application",
        warehouse_binding="warehouse",
        release_version="V1",
    )
    source = await adapter.scaffold(ScaffoldRequest(spec=spec), build)
    artifact = await adapter.build(
        BuildRequest(application_id=app, source=source, spec=spec), build
    )
    plan = await adapter.plan(PlanRequest(artifact=artifact), read)
    deploy = DeployContext(
        tenant_id=tenant,
        operation_id=read.operation_id,
        target=target,
        artifacts=store,
        toolchain_digest=TOOLCHAIN_DIGEST,
        journal=StepJournal(tmp_path / "journal.json", tenant, read.operation_id),
    )
    return adapter, vendor, artifact, plan, deploy, build, read


async def test_package_upload_and_version_are_followed_by_install_and_verified_health(
    prepared_snowflake,
):
    from shifde_contracts.execution import ApplyRequest, ObserveRequest, VerifyRequest

    adapter, vendor, artifact, plan, deploy, *_ = prepared_snowflake
    receipt = await adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    assert [v[0] for v in vendor.mutations] == [
        "package",
        "schema",
        "stage",
        "upload",
        "validate",
        "version",
        "install",
        "grant",
    ]
    assert (await adapter.observe(ObserveRequest(receipt=receipt), deploy)).state == "succeeded"
    assert (await adapter.verify(VerifyRequest(receipt=receipt), deploy)).passed
    assert {r.key for r in receipt.resources} == {"package", "stage", "release-V1-0", "application"}


async def test_failed_setup_validation_cannot_create_version_or_install(prepared_snowflake):
    from shifde_contracts.execution import ApplyRequest

    adapter, vendor, artifact, plan, deploy, *_ = prepared_snowflake
    vendor.fail_validation = True
    with pytest.raises(ValueError, match="validation failed"):
        await adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    assert vendor.packages and vendor.files and not vendor.versions and not vendor.applications
    saved = deploy.journal.completed("stage-create")
    assert saved["resources"] and saved["plan_digest"] == plan.digest


async def test_unknown_install_acceptance_does_not_retry_native_install(prepared_snowflake):
    from shifde_contracts.execution import ApplyRequest
    from shifde_core.journal import NeedsAttention

    adapter, vendor, artifact, plan, deploy, *_ = prepared_snowflake
    vendor.fail_after_install = True
    with pytest.raises(TimeoutError):
        await adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    assert vendor.applications
    with pytest.raises(NeedsAttention):
        await adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    assert len([m for m in vendor.mutations if m[0] == "install"]) == 1


async def upgrade_input(prepared, receipt, *, version="V1", patch=1):
    from dataclasses import replace

    from shifde_contracts.plans import PlanRequest
    from shifde_core.artifact_store import create_archive
    from shifde_core.journal import StepJournal

    adapter, vendor, artifact, _, deploy, build, read = prepared
    spec = artifact.spec.model_copy(update={"release_version": version, "release_patch": patch})
    source = await adapter.scaffold(ScaffoldRequest(spec=spec), build)
    files = read_archive(build.artifacts.read(source, build.tenant_id))
    files["ui/streamlit_app.py"] += f"\n# release {version} patch {patch}\n".encode()
    source = build.artifacts.put(build.tenant_id, create_archive(files))
    next_artifact = await adapter.build(
        BuildRequest(application_id=artifact.application_id, spec=spec, source=source), build
    )
    next_read = replace(read, operation_id=uuid4(), owned_resources=tuple(receipt.resources))
    plan = await adapter.plan(PlanRequest(artifact=next_artifact), next_read)
    next_deploy = replace(
        deploy,
        operation_id=next_read.operation_id,
        owned_resources=next_read.owned_resources,
        journal=StepJournal(
            build.artifacts.root / (str(next_read.operation_id) + ".json"),
            read.tenant_id,
            next_read.operation_id,
        ),
    )
    return next_artifact, plan, next_deploy


async def test_patch_and_new_version_upgrade_preserve_installed_native_identity(prepared_snowflake):
    from shifde_contracts.execution import ApplyRequest, VerifyRequest

    adapter, vendor, artifact, plan, deploy, *_ = prepared_snowflake
    first = await adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    original_id = next(r.native_id for r in first.resources if r.key == "application")
    for version, patch in (("V1", 1), ("V2", 0)):
        artifact, plan, context = await upgrade_input(
            prepared_snowflake, first, version=version, patch=patch
        )
        first = await adapter.apply(ApplyRequest(artifact=artifact, plan=plan), context)
        assert next(r.native_id for r in first.resources if r.key == "application") == original_id
        assert vendor.applications["TEST_APP"]["version"] == version
        assert vendor.applications["TEST_APP"]["patch"] == patch
        assert (await adapter.verify(VerifyRequest(receipt=first), context)).passed
    assert len([m for m in vendor.mutations if m[0] == "install"]) == 1
    assert len([m for m in vendor.mutations if m[0] == "upgrade"]) == 2
    assert not any("DROP " in sql or "CREATE OR REPLACE APPLICATION " in sql for sql in vendor.sql)


async def test_incompatible_upgrade_leaves_previous_app_intact(prepared_snowflake):
    from shifde_contracts.execution import ApplyRequest

    adapter, vendor, artifact, plan, deploy, *_ = prepared_snowflake
    receipt = await adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    before = dict(vendor.applications["TEST_APP"])
    artifact, plan, context = await upgrade_input(prepared_snowflake, receipt)
    vendor.reject_upgrade = True
    with pytest.raises(RuntimeError, match="incompatible upgrade"):
        await adapter.apply(ApplyRequest(artifact=artifact, plan=plan), context)
    assert vendor.applications["TEST_APP"] == before
    assert context.journal.completed("version-create") is not None
    assert context.journal.completed("application-apply") is None


async def test_existing_unowned_package_is_not_adopted(prepared_snowflake):
    from shifde_contracts.execution import ApplyRequest
    from shifde_contracts.plans import PlanRequest

    adapter, vendor, artifact, plan, deploy, _, read = prepared_snowflake
    await adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    with pytest.raises(PermissionError, match="unowned"):
        await adapter.plan(PlanRequest(artifact=artifact), read)


async def test_foreign_object_appearing_mid_apply_is_not_recorded_as_owned(prepared_snowflake):
    from shifde_contracts.execution import ApplyRequest
    from shifde_core.journal import NeedsAttention

    adapter, vendor, artifact, plan, deploy, *_ = prepared_snowflake
    original = vendor.snapshot

    async def external_writer(names):
        if vendor.packages and not vendor.applications:
            vendor.applications["TEST_APP"] = dict(
                name="TEST_APP",
                created_on="foreign-created",
                source="OTHER_PACKAGE",
                source_type="APPLICATION PACKAGE",
                owner="FOREIGN_ROLE",
                comment="foreign",
                version="V1",
                patch=0,
                label="foreign",
                upgrade_state="COMPLETE",
            )
        return await original(names)

    vendor.snapshot = external_writer
    with pytest.raises(NeedsAttention, match="object"):
        await adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    assert deploy.journal.completed("package-create") is None


@pytest.mark.parametrize("replacement", [False, True])
async def test_release_cannot_be_adopted_or_replaced_during_apply(prepared_snowflake, replacement):
    from shifde_contracts.execution import ApplyRequest
    from shifde_core.journal import NeedsAttention

    adapter, vendor, artifact, plan, deploy, *_ = prepared_snowflake
    if replacement:
        receipt = await adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
        from dataclasses import replace

        from shifde_contracts.plans import PlanRequest
        from shifde_core.journal import StepJournal

        read = replace(
            prepared_snowflake[-1], operation_id=uuid4(), owned_resources=tuple(receipt.resources)
        )
        plan = await adapter.plan(PlanRequest(artifact=artifact), read)
        deploy = replace(
            deploy,
            operation_id=read.operation_id,
            owned_resources=read.owned_resources,
            journal=StepJournal(
                deploy.artifacts.root / "replacement.json", read.tenant_id, read.operation_id
            ),
        )
    original = vendor.snapshot
    calls = 0

    async def external_writer(names):
        nonlocal calls
        calls += 1
        if calls == 2:
            vendor.versions[names["package"]] = [
                dict(
                    version="V1",
                    patch=0,
                    created_on="foreign-created",
                    label=plan.details["label"],
                    state="READY",
                    dropped_on=None,
                )
            ]
        return await original(names)

    vendor.snapshot = external_writer
    with pytest.raises(NeedsAttention, match="release"):
        await adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    assert deploy.journal.completed("package-create") is None

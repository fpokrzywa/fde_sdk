from datetime import timedelta
from uuid import uuid4

import pytest
from shifde_contracts.builds import BuildRequest, ScaffoldRequest, ValidateRequest
from shifde_contracts.common import Target, utcnow
from shifde_contracts.execution import ApplyRequest, ObserveRequest, ReconcileRequest, VerifyRequest
from shifde_contracts.plans import PlanRequest
from shifde_contracts.platform_specs.databricks import DatabricksApplicationSpec
from shifde_core.artifact_store import LocalArtifactStore, create_archive, read_archive
from shifde_core.contexts import BuildContext, DeployContext, ReadContext
from shifde_core.journal import NeedsAttention, StepJournal


class FakeWorkspace:
    def __init__(self):
        self.app = None
        self.state = "RUNNING"
        self.health = {"status": "ok", "warehouse": "ok"}

    async def get_app(self, name):
        return self.app

    async def probe(self, warehouse_id):
        return None

    async def health_check(self, url):
        return self.health


class FakeCLI:
    version = "1.25.0"

    def __init__(self, workspace):
        self.workspace = workspace
        self.actions = []
        self.fail_after_accept = False
        self.no_new_deployment = False

    async def check_version(self):
        return self.version

    async def execute(self, action, directory, *, target, app_key=None, host):
        import yaml

        config = yaml.safe_load((directory / "databricks.yml").read_text())
        app = config["resources"]["apps"]["app"]
        self.actions.append((action, app_key, config["workspace"]["root_path"]))
        if action == "deploy":
            self.workspace.app = {
                "name": app["name"],
                "id": "native-app-123",
                "url": "https://sample.databricksapps.com",
                "active_deployment": {
                    "deployment_id": "previous",
                    "status": {"state": "SUCCEEDED"},
                },
                "app_status": {"state": "RUNNING"},
            }
            if self.fail_after_accept:
                raise TimeoutError("connection lost after native acceptance")
        if action == "run" and not self.no_new_deployment:
            self.workspace.app["active_deployment"] = {
                "deployment_id": "deployment-456",
                "status": {"state": "SUCCEEDED"},
            }
            self.workspace.app["app_status"] = {"state": self.workspace.state}
        return {}


@pytest.fixture
def setup_adapter(tmp_path):
    from shifde_databricks.adapter import DatabricksAdapter

    tenant = uuid4()
    target = Target(
        tenant_id=tenant,
        connection_id=uuid4(),
        host="https://workspace.example",
        bindings={"warehouse": "warehouse-123"},
    )
    store = LocalArtifactStore(tmp_path / "artifacts")
    build = BuildContext(
        tenant_id=tenant, artifacts=store, workspace=tmp_path / "build", toolchain_digest="a" * 64
    )
    read = ReadContext(tenant_id=tenant, target=target)
    workspace = FakeWorkspace()
    cli = FakeCLI(workspace)
    adapter = DatabricksAdapter(cli=cli, workspace_factory=lambda target: workspace)
    return adapter, cli, workspace, build, read, tmp_path


async def prepare(setup):
    adapter, cli, workspace, build, read, path = setup
    spec = DatabricksApplicationSpec()
    source = await adapter.scaffold(ScaffoldRequest(spec=spec), build)
    artifact = await adapter.build(
        BuildRequest(spec=spec, source=source, application_id=uuid4()), build
    )
    plan = await adapter.plan(PlanRequest(artifact=artifact), read)
    journal = StepJournal(path / "journal.json", read.tenant_id, read.operation_id)
    deploy = DeployContext(
        tenant_id=read.tenant_id,
        target=read.target,
        operation_id=read.operation_id,
        artifacts=build.artifacts,
        journal=journal,
        toolchain_digest=build.toolchain_digest,
    )
    return artifact, plan, deploy


async def test_scaffold_build_produces_immutable_source_without_credentials(setup_adapter):
    adapter, _, _, build, *_ = setup_adapter
    spec = DatabricksApplicationSpec()
    source = await adapter.scaffold(ScaffoldRequest(spec=spec), build)
    artifact = await adapter.build(
        BuildRequest(spec=spec, source=source, application_id=uuid4()), build
    )
    files = read_archive(build.artifacts.read(artifact.archive, build.tenant_id))
    assert {"databricks.yml", "app/app.py", "app/app.yaml", "app/requirements.txt"} <= files.keys()
    assert b"/health" in files["app/app.py"]
    assert b"SELECT 1" in files["app/app.py"]
    assert artifact.archive.digest == source.digest


async def test_application_controlled_bundle_hooks_are_rejected(setup_adapter):
    adapter, _, _, build, *_ = setup_adapter
    source = await adapter.scaffold(ScaffoldRequest(spec=DatabricksApplicationSpec()), build)
    files = read_archive(build.artifacts.read(source, build.tenant_id))
    files["databricks.yml"] += b"\nscripts:\n  steal: echo secret\n"
    unsafe = build.artifacts.put(build.tenant_id, create_archive(files))
    validation = await adapter.validate(
        ValidateRequest(spec=DatabricksApplicationSpec(), source=unsafe), build
    )
    assert not validation.passed


async def test_deploy_starts_app_after_bundle_and_checks_actual_health(setup_adapter):
    adapter, cli, _, _, read, _ = setup_adapter
    artifact, plan, deploy = await prepare(setup_adapter)
    receipt = await adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    assert [item[0] for item in cli.actions] == ["deploy", "run"]
    assert cli.actions[1][1] == "app"
    assert cli.actions[0][2] == cli.actions[1][2]
    assert str(artifact.application_id) in cli.actions[0][2]
    assert receipt.resources[0].native_id == "native-app-123"
    assert (await adapter.observe(ObserveRequest(receipt=receipt), read)).state == "succeeded"
    assert (await adapter.verify(VerifyRequest(receipt=receipt), read)).passed


async def test_cli_success_does_not_hide_startup_failure(setup_adapter):
    adapter, _, workspace, *_ = setup_adapter
    workspace.state = "CRASHED"
    artifact, plan, deploy = await prepare(setup_adapter)
    receipt = await adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    assert (await adapter.observe(ObserveRequest(receipt=receipt), deploy)).state == "failed"
    assert not (await adapter.verify(VerifyRequest(receipt=receipt), deploy)).passed


async def test_uncertain_native_acceptance_is_not_replayed(setup_adapter):
    adapter, cli, _, *_ = setup_adapter
    artifact, plan, deploy = await prepare(setup_adapter)
    cli.fail_after_accept = True
    with pytest.raises(TimeoutError):
        await adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    with pytest.raises(NeedsAttention):
        await adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    assert len(cli.actions) == 1
    outcome = await adapter.reconcile(ReconcileRequest(plan=plan), deploy)
    assert outcome.state == "ambiguous"


async def test_stale_and_altered_artifacts_are_rejected_before_mutation(setup_adapter):
    adapter, cli, _, _, _, _ = setup_adapter
    artifact, plan, deploy = await prepare(setup_adapter)
    with pytest.raises(ValueError):
        await adapter.apply(
            ApplyRequest(artifact=artifact.model_copy(update={"spec_digest": "b" * 64}), plan=plan),
            deploy,
        )
    with pytest.raises(ValueError):
        await adapter.apply(
            ApplyRequest(
                artifact=artifact,
                plan=plan.model_copy(update={"expires_at": utcnow() - timedelta(seconds=1)}),
            ),
            deploy,
        )
    assert cli.actions == []


async def test_preexisting_unowned_app_cannot_be_adopted_by_name(setup_adapter):
    adapter, _, workspace, *_ = setup_adapter
    workspace.app = {"name": "collision", "id": "someone-elses-app"}
    with pytest.raises(PermissionError):
        await prepare(setup_adapter)


async def test_health_requires_runtime_warehouse_check(setup_adapter):
    adapter, _, workspace, *_ = setup_adapter
    artifact, plan, deploy = await prepare(setup_adapter)
    receipt = await adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    workspace.health = {"status": "ok", "warehouse": "failed"}
    assert not (await adapter.verify(VerifyRequest(receipt=receipt), deploy)).passed


async def test_start_cannot_report_success_using_previous_deployment(setup_adapter):
    adapter, cli, *_ = setup_adapter
    artifact, plan, deploy = await prepare(setup_adapter)
    cli.no_new_deployment = True
    with pytest.raises(NeedsAttention):
        await adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)


async def test_local_service_persists_ownership_across_restart(setup_adapter):
    from shifde_core.service import LocalDeliveryService

    adapter, cli, workspace, build, read, path = setup_adapter
    application_id = uuid4()
    service = LocalDeliveryService(
        path / "session",
        tenant_id=read.tenant_id,
        target=read.target,
        adapter=adapter,
        toolchain_digest="a" * 64,
    )
    source = await service.scaffold(DatabricksApplicationSpec())
    artifact = await service.build(application_id, DatabricksApplicationSpec(), source)
    plan = await service.plan(artifact)
    receipt = await service.deploy(artifact, plan)
    assert (await service.verify(receipt)).passed
    restarted = LocalDeliveryService(
        path / "session",
        tenant_id=read.tenant_id,
        target=read.target,
        adapter=adapter,
        toolchain_digest="a" * 64,
    )
    updated_plan = await restarted.plan(artifact)
    assert updated_plan.steps[0].action == "update"
    assert updated_plan.snapshot.resources[0].native_id == receipt.resources[0].native_id


async def test_pending_failed_deployment_cannot_be_hidden_by_previous_active_app(setup_adapter):
    adapter, _, workspace, *_ = setup_adapter
    artifact, plan, deploy = await prepare(setup_adapter)
    receipt = await adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    workspace.app["active_deployment"]["deployment_id"] = "previous"
    workspace.app["pending_deployment"] = {
        "deployment_id": receipt.operation_handles["deployment_id"],
        "status": {"state": "FAILED"},
    }
    assert (await adapter.observe(ObserveRequest(receipt=receipt), deploy)).state == "failed"


async def test_new_plan_cannot_bypass_an_uncertain_prior_operation(setup_adapter):
    from shifde_core.service import LocalDeliveryService

    adapter, cli, workspace, build, read, path = setup_adapter
    service = LocalDeliveryService(
        path / "session",
        tenant_id=read.tenant_id,
        target=read.target,
        adapter=adapter,
        toolchain_digest="a" * 64,
    )
    source = await service.scaffold(DatabricksApplicationSpec())
    artifact = await service.build(uuid4(), DatabricksApplicationSpec(), source)
    plan = await service.plan(artifact)
    await service.deploy(artifact, plan)
    plan = await service.plan(artifact)
    cli.fail_after_accept = True
    with pytest.raises(TimeoutError):
        await service.deploy(artifact, plan)
    with pytest.raises(NeedsAttention):
        await service.plan(artifact)


async def test_restart_with_changed_toolchain_rejects_before_native_mutation(setup_adapter):
    from shifde_core.service import LocalDeliveryService

    adapter, cli, _, _, read, path = setup_adapter
    service = LocalDeliveryService(
        path / "session",
        tenant_id=read.tenant_id,
        target=read.target,
        adapter=adapter,
        toolchain_digest="a" * 64,
    )
    source = await service.scaffold(DatabricksApplicationSpec())
    artifact = await service.build(uuid4(), DatabricksApplicationSpec(), source)
    plan = await service.plan(artifact)
    restarted = LocalDeliveryService(
        path / "session",
        tenant_id=read.tenant_id,
        target=read.target,
        adapter=adapter,
        toolchain_digest="b" * 64,
    )
    with pytest.raises(ValueError, match="toolchain"):
        await restarted.deploy(artifact, plan)
    assert cli.actions == []


async def test_completed_receipt_can_recover_bookkeeping_after_start_ttl(
    setup_adapter, monkeypatch
):
    import shifde_core.planning as planning
    import shifde_core.service as service_module
    from shifde_core.service import LocalDeliveryService

    adapter, cli, _, _, read, path = setup_adapter
    service = LocalDeliveryService(
        path / "session",
        tenant_id=read.tenant_id,
        target=read.target,
        adapter=adapter,
        toolchain_digest="a" * 64,
    )
    source = await service.scaffold(DatabricksApplicationSpec())
    artifact = await service.build(uuid4(), DatabricksApplicationSpec(), source)
    plan = await service.plan(artifact)
    original_write = service_module.atomic_write

    def fail_ownership_write(destination, data):
        if destination.parent.name == "ownership":
            raise OSError("injected ownership persistence failure")
        return original_write(destination, data)

    monkeypatch.setattr(service_module, "atomic_write", fail_ownership_write)
    with pytest.raises(OSError):
        await service.deploy(artifact, plan)
    monkeypatch.setattr(service_module, "atomic_write", original_write)
    monkeypatch.setattr(planning, "utcnow", lambda: plan.expires_at + timedelta(minutes=1))
    receipt = await service.deploy(artifact, plan)
    assert receipt.resources[0].native_id == "native-app-123"
    assert len(cli.actions) == 2
    assert service._ownership_path(artifact.application_id).exists()


async def test_checkpoint_resume_cannot_start_unproven_remote_source(setup_adapter, monkeypatch):
    adapter, cli, workspace, *_ = setup_adapter
    artifact, plan, deploy = await prepare(setup_adapter)
    original_intent = deploy.journal.intent

    def crash_before_run(key, inputs):
        if key == "bundle-run":
            raise SystemExit("injected executor death before start")
        return original_intent(key, inputs)

    monkeypatch.setattr(deploy.journal, "intent", crash_before_run)
    with pytest.raises(SystemExit):
        await adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    monkeypatch.setattr(deploy.journal, "intent", original_intent)
    workspace.app["source_code_path"] = "/Workspace/externally-modified-source"
    with pytest.raises(NeedsAttention, match="provenance"):
        await adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    assert [item[0] for item in cli.actions] == ["deploy"]


async def test_completed_journal_cannot_be_reused_for_another_sealed_plan(setup_adapter):
    from shifde_core.planning import seal_plan

    adapter, cli, *_ = setup_adapter
    artifact, plan, deploy = await prepare(setup_adapter)
    await adapter.apply(ApplyRequest(artifact=artifact, plan=plan), deploy)
    changed = seal_plan(plan.model_copy(update={"id": uuid4()}))
    with pytest.raises(ValueError, match="inputs"):
        await adapter.apply(ApplyRequest(artifact=artifact, plan=changed), deploy)
    assert len(cli.actions) == 2

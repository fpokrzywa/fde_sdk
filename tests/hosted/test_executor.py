import pytest
from shifde_contracts.hosted import (
    ApplicationSubmission,
    ConnectionSubmission,
    ProjectSubmission,
    ScaffoldSubmission,
    TargetSubmission,
)
from shifde_contracts.platform_specs.databricks import DatabricksApplicationSpec
from shifde_contracts.submissions import BuildSubmission, DeploymentSubmission, PlanSubmission

from tests.adapters.test_databricks import FakeCLI, FakeWorkspace

pytestmark = pytest.mark.integration


@pytest.fixture
def execution_setup(hosted_service):
    from shifde_databricks.adapter import DatabricksAdapter
    from shifde_hosted.executor import OperationExecutor, OperationRef

    service, actor = hosted_service
    host = service.credential_bindings["development"].host
    conn = service.create_connection(
        actor, ConnectionSubmission(name="dev", host=host, credential_ref="development")
    )
    target = service.create_target(
        actor, TargetSubmission(connection_id=conn.id, bindings={"warehouse": "123"})
    )
    workspace = FakeWorkspace()
    cli = FakeCLI(workspace)
    adapter = DatabricksAdapter(cli=cli, workspace_factory=lambda _: workspace)
    executor = OperationExecutor(
        service, adapter_factory=lambda _: adapter, toolchain_digest="a" * 64
    )
    app = service.create_application(
        actor, ApplicationSubmission(name="test", kind="databricks.app/v1")
    )

    def run(kind, request, key):
        op = service.submit(actor, kind, request, key)
        executor.execute(OperationRef(str(actor.tenant_id), str(op.id)))
        return service.get_operation(actor, op.id)

    scaffold = run(
        "scaffold",
        ScaffoldSubmission(application_id=app.id, spec=DatabricksApplicationSpec()),
        "scaffold",
    )
    project = service.create_project(
        actor,
        ProjectSubmission(
            application_id=app.id,
            source_id=scaffold.result.value.id,
            spec=DatabricksApplicationSpec(),
        ),
    )
    build = run("build", BuildSubmission(project_id=project.id), "build")
    plan = run(
        "plan", PlanSubmission(artifact_id=build.result.value.id, target_id=target.id), "plan"
    )
    return service, actor, executor, cli, workspace, build.result.value, plan.result.value


def test_native_receipt_is_not_a_success_until_observed_and_verified(execution_setup):
    from shifde_hosted.executor import OperationRef

    service, actor, executor, cli, workspace, artifact, plan = execution_setup
    op = service.submit(
        actor,
        "deployment",
        DeploymentSubmission(artifact_id=artifact.id, plan_id=plan.id),
        "deploy",
    )
    ref = OperationRef(str(actor.tenant_id), str(op.id))
    assert executor.execute(ref) == "submitted"
    assert service.get_operation(actor, op.id).state == "running"
    assert executor.observe(ref) == "terminal"
    completed = service.get_operation(actor, op.id)
    assert completed.state == "succeeded" and completed.result.value.healthy
    assert [a[0] for a in cli.actions] == ["deploy", "run"]


def test_uncertain_native_write_retains_claim_and_cannot_be_replayed(execution_setup):
    from shifde_hosted.errors import ServiceError
    from shifde_hosted.executor import OperationRef

    service, actor, executor, cli, _, artifact, plan = execution_setup
    cli.fail_after_accept = True
    op = service.submit(
        actor,
        "deployment",
        DeploymentSubmission(artifact_id=artifact.id, plan_id=plan.id),
        "deploy",
    )
    ref = OperationRef(str(actor.tenant_id), str(op.id))
    assert executor.execute(ref) == "terminal"
    assert service.get_operation(actor, op.id).state == "needs_attention"
    executor.execute(ref)
    assert len(cli.actions) == 1
    with pytest.raises(ServiceError, match="UNPROVEN_NATIVE_OUTCOME"):
        service.resume(actor, op.id)


def test_queued_cancellation_never_calls_native_platform(execution_setup):
    from shifde_hosted.executor import OperationRef

    service, actor, executor, cli, _, artifact, plan = execution_setup
    op = service.submit(
        actor,
        "deployment",
        DeploymentSubmission(artifact_id=artifact.id, plan_id=plan.id),
        "deploy",
    )
    service.cancel(actor, op.id)
    assert executor.execute(OperationRef(str(actor.tenant_id), str(op.id))) == "terminal"
    assert service.get_operation(actor, op.id).state == "cancelled"
    assert cli.actions == []

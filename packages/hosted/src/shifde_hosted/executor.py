import asyncio
from uuid import UUID, uuid4, uuid5

import sqlalchemy as sa
from shifde_contracts.builds import BuildArtifact, BuildRequest, ScaffoldRequest, ValidateRequest
from shifde_contracts.common import Diagnostic, ResourceBinding, utcnow
from shifde_contracts.execution import (
    ApplyReceipt,
    ApplyRequest,
    Deployment,
    ObserveRequest,
    VerifyRequest,
)
from shifde_contracts.operations import (
    BuildResult,
    DeploymentResult,
    InspectionResult,
    PlanResult,
    ScaffoldResult,
    ValidationResult,
)
from shifde_contracts.plans import DeploymentPlan, InspectRequest, PlanRequest
from shifde_contracts.platform_specs.snowflake import SnowflakeApplicationSpec
from shifde_contracts.projects import Project
from shifde_contracts.workflow import OperationRef
from shifde_core.contexts import BuildContext, DeployContext, ReadContext
from shifde_core.hashing import digest
from shifde_core.journal import NeedsAttention
from shifde_core.native import FABRIC_PROFILE, SNOWFLAKE_PROFILE, terminal_step
from shifde_core.planning import validate_plan
from sqlalchemy.dialects.postgresql import insert

from shifde_hosted.auth import Principal
from shifde_hosted.journal import CancellationRequested, PostgresJournal
from shifde_hosted.persistence import models as m
from shifde_hosted.persistence import repository as r
from shifde_hosted.service import ApplicationService


class OperationExecutor:
    def __init__(self, service: ApplicationService, *, adapter_factory, toolchain_digest: str):
        self.service, self.database = service, service.database
        self.adapter_factory, self.toolchain_digest = adapter_factory, toolchain_digest

    @staticmethod
    def _actor(row):
        return Principal(row["tenant_id"], row["requester"], frozenset(row["scopes"]))

    def _refs(self, c, row):
        return dict(
            c.execute(
                sa.select(m.operation_links.c.relation, m.operation_links.c.resource_id).where(
                    m.operation_links.c.operation_id == row["id"]
                )
            ).all()
        )

    def _owned(self, c, application_id, target_id):
        return tuple(
            ResourceBinding.model_validate(row.data)
            for row in c.execute(
                sa.select(m.resource_bindings.c.data).where(
                    m.resource_bindings.c.application_id == application_id,
                    m.resource_bindings.c.target_id == target_id,
                )
            )
        )

    def _record_bindings(self, c, receipt):
        for binding in receipt.resources:
            values = dict(
                tenant_id=receipt.tenant_id,
                application_id=receipt.application_id,
                target_id=receipt.target_id,
                key=binding.key,
                native_id=binding.native_id,
                resource_type=binding.resource_type,
                data=binding.model_dump(mode="json"),
            )
            c.execute(
                insert(m.resource_bindings)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=["tenant_id", "application_id", "target_id", "key"],
                    set_={
                        k: v
                        for k, v in values.items()
                        if k not in ("tenant_id", "application_id", "target_id", "key")
                    },
                )
            )

    def _release(self, c, operation_id):
        c.execute(
            m.application_claims.delete().where(m.application_claims.c.operation_id == operation_id)
        )

    def _failure(self, ref: OperationRef, code: str, *, attention: bool = False):
        tenant, identity = UUID(ref.tenant_id), UUID(ref.operation_id)
        with self.database.transaction(tenant) as c:
            row = r.operation(c, tenant, identity, lock=True)
            if row["state"] in r.TERMINAL:
                return "terminal"
            intents = c.execute(
                sa.select(m.step_journal.c.key).where(m.step_journal.c.operation_id == identity)
            ).first()
            state = (
                "needs_attention"
                if attention or (row["kind"] == "deployment" and intents)
                else "failed"
            )
            if row["state"] == "queued":
                r.set_state(c, row, "running")
                row = r.operation(c, tenant, identity, lock=True)
            r.set_state(
                c,
                row,
                state,
                error=Diagnostic(
                    code=code,
                    message="operation requires operator review"
                    if state == "needs_attention"
                    else "operation could not be completed",
                ),
            )
            if state != "needs_attention":
                self._release(c, identity)
        return "terminal"

    def execute(self, ref: OperationRef) -> str:
        try:
            return asyncio.run(self._execute(ref))
        except CancellationRequested:
            tenant, identity = UUID(ref.tenant_id), UUID(ref.operation_id)
            with self.database.transaction(tenant) as c:
                row = r.operation(c, tenant, identity, lock=True)
                intents = c.execute(
                    sa.select(m.step_journal.c.key).where(m.step_journal.c.operation_id == identity)
                ).first()
                if not intents:
                    r.set_state(c, row, "cancelled")
                    self._release(c, identity)
                    return "terminal"
            return self._failure(ref, "CANCELLATION_WITH_NATIVE_EFFECTS", attention=True)
        except NeedsAttention:
            return self._failure(ref, "NATIVE_OUTCOME_UNPROVEN", attention=True)
        except Exception:
            return self._failure(ref, "EXECUTION_FAILED")

    async def _execute(self, ref: OperationRef):
        tenant, identity = UUID(ref.tenant_id), UUID(ref.operation_id)
        execution_id = uuid4()
        saved = None
        with self.database.transaction(tenant) as c:
            row = r.operation(c, tenant, identity, lock=True)
            if row["state"] in r.TERMINAL or row["state"] == "needs_attention":
                return "terminal"
            action = (
                "deploy"
                if row["kind"] == "deployment"
                else "read"
                if row["kind"] == "inspection"
                else "write"
            )
            r.authorize(c, self._actor(row), action)
            if row["deadline"] <= utcnow():
                raise TimeoutError("operation expired")
            if row["cancel_requested"] and row["state"] in ("queued", "waiting_for_policy"):
                r.set_state(c, row, "cancelled")
                return "terminal"
            if row["state"] == "waiting_for_policy":
                approved = c.execute(
                    sa.select(m.resources.c.data).where(
                        m.resources.c.kind == "policy_decision",
                        m.resources.c.data["operation_id"].as_string() == str(identity),
                    )
                ).scalar()
                if not approved:
                    return "waiting"
                if not approved["approve"]:
                    return "terminal"
            refs = self._refs(c, row)
            application_kind = r.resource(c, tenant, "application", refs["application"])["data"][
                "kind"
            ]
            target = self.service.target(c, tenant, refs["target"]) if "target" in refs else None
            artifact = (
                BuildArtifact.model_validate(
                    r.resource(c, tenant, "artifact", refs["artifact"])["data"]
                )
                if "artifact" in refs
                else None
            )
            plan = (
                DeploymentPlan.model_validate(r.resource(c, tenant, "plan", refs["plan"])["data"])
                if "plan" in refs
                else None
            )
            project = (
                Project.model_validate(r.resource(c, tenant, "project", refs["project"])["data"])
                if "project" in refs
                else None
            )
            owned = self._owned(c, refs["application"], target.id) if target else ()
            if row["kind"] == "deployment":
                assert target is not None and artifact is not None and plan is not None
                # Lock the target before checking/inserting a claim, matching PATCH ordering.
                r.resource(c, tenant, "target", target.id, lock=True)
                target = self.service.target(c, tenant, target.id)
                owned = self._owned(c, refs["application"], target.id)
                saved = c.execute(
                    sa.select(m.step_journal.c.receipt).where(
                        m.step_journal.c.operation_id == identity,
                        m.step_journal.c.key == terminal_step(artifact.spec.kind),
                        m.step_journal.c.state == "complete",
                    )
                ).scalar()
                if row["execution_id"] is not None and not saved:
                    raise NeedsAttention("another execution may have submitted native work")
                if not saved:
                    validate_plan(plan, target)
                    if self.service.requires_approval(c, tenant, target):
                        decision = c.execute(
                            sa.select(m.resources.c.data).where(
                                m.resources.c.kind == "policy_decision",
                                m.resources.c.data["operation_id"].as_string() == str(identity),
                            )
                        ).scalar()
                        if (
                            not decision
                            or not decision["approve"]
                            or decision["plan_digest"] != plan.digest
                        ):
                            if row["state"] == "queued":
                                r.set_state(c, row, "running")
                                row = r.operation(c, tenant, identity, lock=True)
                            if row["state"] != "waiting_for_policy":
                                r.set_state(c, row, "waiting_for_policy")
                            return "waiting"
                claim = (
                    c.execute(
                        sa.select(m.application_claims)
                        .where(
                            m.application_claims.c.application_id == refs["application"],
                            m.application_claims.c.target_id == target.id,
                        )
                        .with_for_update()
                    )
                    .mappings()
                    .first()
                )
                if claim and claim["operation_id"] != identity:
                    raise NeedsAttention("application has an unresolved operation")
                fence = row["fence"] + 1
                values = dict(
                    tenant_id=tenant,
                    application_id=refs["application"],
                    target_id=target.id,
                    operation_id=identity,
                    execution_id=execution_id,
                    fence=fence,
                )
                c.execute(
                    insert(m.application_claims)
                    .values(**values)
                    .on_conflict_do_update(
                        index_elements=["tenant_id", "application_id", "target_id"],
                        set_={"execution_id": execution_id, "fence": fence},
                    )
                )
            else:
                fence = row["fence"] + 1
            if row["state"] != "running":
                r.set_state(c, row, "running")
            c.execute(
                m.operations.update()
                .where(m.operations.c.id == identity)
                .values(
                    execution_id=execution_id, fence=fence, started_at=row["started_at"] or utcnow()
                )
            )
            deadline = row["deadline"]
            kind, body = row["kind"], row["request"]
        adapter = self.adapter_factory(target)
        toolchain_digest = (
            digest(FABRIC_PROFILE)
            if application_kind in {"fabric.solution/v1", "fabric.iq/v1"}
            else digest(SNOWFLAKE_PROFILE)
            if application_kind == "snowflake.native-app/v1"
            else self.toolchain_digest
        )
        build_context = BuildContext(
            tenant_id=tenant,
            operation_id=identity,
            artifacts=self.service.artifacts,
            workspace=self.service.artifacts.root / "build" / str(identity),
            toolchain_digest=toolchain_digest,
        )
        read_context = (
            ReadContext(
                tenant_id=tenant,
                operation_id=identity,
                target=target,
                owned_resources=owned,
                deadline=deadline,
            )
            if target
            else None
        )
        if kind == "deployment":
            assert target is not None and artifact is not None and plan is not None
            if saved:
                receipt = ApplyReceipt.model_validate(saved)
                if (
                    receipt.tenant_id != tenant
                    or receipt.target_id != target.id
                    or receipt.application_id != artifact.application_id
                    or receipt.artifact_digest != artifact.archive.digest
                ):
                    raise NeedsAttention("saved receipt binding mismatch")
            else:
                journal = PostgresJournal(self.database, tenant, identity, execution_id, fence)
                context = DeployContext(
                    tenant_id=tenant,
                    operation_id=identity,
                    target=target,
                    owned_resources=owned,
                    deadline=deadline,
                    artifacts=self.service.artifacts,
                    journal=journal,
                    toolchain_digest=toolchain_digest,
                    write_guard=journal.guard,
                )
                receipt = await adapter.apply(ApplyRequest(artifact=artifact, plan=plan), context)
            with self.database.transaction(tenant) as c:
                current = r.operation(c, tenant, identity, lock=True)
                if current["execution_id"] != execution_id:
                    raise NeedsAttention("stale execution")
                existing = c.execute(
                    sa.select(m.resources.c.id).where(m.resources.c.id == receipt.id)
                ).first()
                if not existing:
                    r.put_resource(c, tenant, "receipt", receipt, links={"deployment": identity})
                c.execute(
                    insert(m.operation_links)
                    .values(
                        tenant_id=tenant,
                        operation_id=identity,
                        relation="receipt",
                        resource_id=receipt.id,
                    )
                    .on_conflict_do_nothing()
                )
                self._record_bindings(c, receipt)
                deployment = Deployment(
                    id=identity,
                    tenant_id=tenant,
                    application_id=receipt.application_id,
                    target_id=receipt.target_id,
                    artifact_digest=receipt.artifact_digest,
                    resources=receipt.resources,
                    healthy=False,
                )
                r.replace_resource(c, tenant, identity, deployment)
                r.event(c, tenant, identity, "native_submitted")
            return "submitted"
        if kind == "scaffold":
            result: (
                ScaffoldResult | ValidationResult | BuildResult | PlanResult | InspectionResult
            ) = ScaffoldResult(
                value=await adapter.scaffold(
                    ScaffoldRequest(spec=body["spec"], template_version=body["template_version"]),
                    build_context,
                )
            )
            storage_kind = "source"
        elif kind == "validation":
            assert project is not None
            result = ValidationResult(
                value=await adapter.validate(
                    ValidateRequest(spec=project.spec, source=project.source), build_context
                )
            )
            storage_kind = None
        elif kind == "build":
            assert project is not None
            result = BuildResult(
                value=await adapter.build(
                    BuildRequest(
                        application_id=project.application_id,
                        spec=project.spec,
                        source=project.source,
                    ),
                    build_context,
                )
            )
            storage_kind = "artifact"
        elif kind == "plan":
            assert artifact is not None and read_context is not None
            result = PlanResult(
                value=await adapter.plan(PlanRequest(artifact=artifact), read_context)
            )
            storage_kind = "plan"
        elif kind == "inspection":
            assert target is not None and read_context is not None
            from shifde_databricks.adapter import app_name

            report = await adapter.capabilities(read_context)
            result = InspectionResult(
                value=await adapter.inspect(
                    InspectRequest(
                        application_id=refs["application"],
                        app_name=target.bindings[project.spec.application_binding]
                        if project is not None
                        and isinstance(project.spec, SnowflakeApplicationSpec)
                        else app_name(refs["application"], target.id),
                        spec=project.spec if project is not None else None,
                    ),
                    read_context,
                )
            )
            storage_kind = None
            with self.database.transaction(tenant) as c:
                cid = uuid5(target.id, "capabilities")
                c.execute(
                    insert(m.resources)
                    .values(
                        tenant_id=tenant,
                        id=cid,
                        kind="capabilities",
                        data=report.model_dump(mode="json"),
                    )
                    .on_conflict_do_update(
                        index_elements=["tenant_id", "id"],
                        set_={"data": report.model_dump(mode="json")},
                    )
                )
        else:
            raise ValueError("unsupported operation kind")
        with self.database.transaction(tenant) as c:
            current = r.operation(c, tenant, identity, lock=True)
            if current["execution_id"] != execution_id:
                return "terminal"
            if current["cancel_requested"]:
                r.set_state(c, current, "cancelled")
                return "terminal"
            if storage_kind:
                links = {
                    k: v for k, v in refs.items() if k in ("application", "target", "artifact")
                }
                r.put_resource(c, tenant, storage_kind, result.value, links=links)
            r.set_state(c, current, "succeeded", result=result)
        return "terminal"

    def observe(self, ref: OperationRef) -> str:
        try:
            return asyncio.run(self._observe(ref))
        except Exception:
            return self._failure(ref, "OBSERVATION_FAILED", attention=True)

    async def _observe(self, ref: OperationRef):
        tenant, identity = UUID(ref.tenant_id), UUID(ref.operation_id)
        with self.database.transaction(tenant) as c:
            row = r.operation(c, tenant, identity)
            if row["state"] in r.TERMINAL or row["state"] == "needs_attention":
                return "terminal"
            r.authorize(c, self._actor(row), "deploy")
            if row["deadline"] <= utcnow():
                raise TimeoutError("native observation deadline")
            refs = self._refs(c, row)
            receipt = ApplyReceipt.model_validate(
                r.resource(c, tenant, "receipt", refs["receipt"])["data"]
            )
            artifact = BuildArtifact.model_validate(
                r.resource(c, tenant, "artifact", refs["artifact"])["data"]
            )
            target = self.service.target(c, tenant, receipt.target_id)
            context = ReadContext(
                tenant_id=tenant,
                operation_id=identity,
                target=target,
                deadline=row["deadline"],
                owned_resources=self._owned(c, receipt.application_id, target.id),
            )
            execution_id = row["execution_id"]
        adapter = self.adapter_factory(target)
        observed = await adapter.observe(
            ObserveRequest(receipt=receipt, spec=artifact.spec), context
        )
        if observed.state in ("running", "unknown"):
            return "submitted"
        healthy = False
        if observed.state == "succeeded" and not row["cancel_requested"]:
            healthy = (
                await adapter.verify(VerifyRequest(receipt=receipt, spec=artifact.spec), context)
            ).passed
        with self.database.transaction(tenant) as c:
            current = r.operation(c, tenant, identity, lock=True)
            if current["execution_id"] != execution_id or current["state"] in r.TERMINAL:
                return "terminal"
            deployment = Deployment(
                id=identity,
                tenant_id=tenant,
                application_id=receipt.application_id,
                target_id=target.id,
                artifact_digest=receipt.artifact_digest,
                resources=receipt.resources,
                healthy=healthy and not current["cancel_requested"],
            )
            r.replace_resource(c, tenant, identity, deployment)
            state = (
                "cancelled"
                if current["cancel_requested"]
                else "succeeded"
                if deployment.healthy
                else "failed"
            )
            r.set_state(
                c,
                current,
                state,
                result=DeploymentResult(value=deployment),
                error=None
                if deployment.healthy
                else Diagnostic(
                    code="CANCELLED_WITH_NATIVE_EFFECTS"
                    if state == "cancelled"
                    else "VERIFICATION_FAILED",
                    message="native deployment exists; inspect its recorded state",
                ),
            )
            self._release(c, identity)
        return "terminal"

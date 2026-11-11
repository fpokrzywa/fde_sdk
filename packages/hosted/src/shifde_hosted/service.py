import hashlib
import zipfile
from datetime import timedelta
from uuid import UUID, uuid4, uuid5

import sqlalchemy as sa
from pydantic import BaseModel
from shifde_contracts.builds import BuildArtifact
from shifde_contracts.capabilities import Capability, CapabilityReport
from shifde_contracts.common import Diagnostic, SourceArchive, Target, utcnow
from shifde_contracts.execution import Deployment
from shifde_contracts.hosted import (
    ApplicationSubmission,
    Connection,
    ConnectionSubmission,
    DeploymentView,
    EventPage,
    InspectionSubmission,
    OperationEvent,
    PolicyDecisionSubmission,
    ProjectSubmission,
    ScaffoldSubmission,
    SourceUpload,
    SourceUploadSubmission,
    TargetPatch,
    TargetSubmission,
    ValidationSubmission,
)
from shifde_contracts.operations import Operation
from shifde_contracts.plans import DeploymentPlan
from shifde_contracts.projects import Application, Project
from shifde_contracts.submissions import BuildSubmission, DeploymentSubmission, PlanSubmission
from shifde_core.artifact_store import ArtifactStore
from shifde_core.hashing import digest
from shifde_core.native import TERMINAL_STEPS, terminal_step
from shifde_core.planning import validate_plan
from sqlalchemy.dialects.postgresql import insert

from shifde_hosted.auth import Principal
from shifde_hosted.config import CredentialBinding
from shifde_hosted.errors import Forbidden, NotFound, ServiceError
from shifde_hosted.persistence import models as m
from shifde_hosted.persistence import repository as r
from shifde_hosted.persistence.database import Database

SUBMISSIONS: dict[str, type[BaseModel]] = {
    "scaffold": ScaffoldSubmission,
    "inspection": InspectionSubmission,
    "validation": ValidationSubmission,
    "build": BuildSubmission,
    "plan": PlanSubmission,
    "deployment": DeploymentSubmission,
}
MODEL_KINDS: dict[str, type[BaseModel]] = {
    "application": Application,
    "connection": Connection,
    "target": Target,
    "project": Project,
    "source": SourceArchive,
    "artifact": BuildArtifact,
    "plan": DeploymentPlan,
    "deployment": Deployment,
}


class ApplicationService:
    def __init__(
        self,
        database: Database,
        artifacts: ArtifactStore,
        *,
        credential_bindings: dict[str, CredentialBinding],
        require_approval_environments: tuple[str, ...] = ("prod",),
        operation_timeout_seconds: int = 3600,
    ):
        self.database, self.artifacts = database, artifacts
        self.credential_bindings = credential_bindings
        self.require_approval_environments = require_approval_environments
        self.operation_timeout_seconds = operation_timeout_seconds

    def create_application(self, actor: Principal, request: ApplicationSubmission) -> Application:
        model = Application(tenant_id=actor.tenant_id, **request.model_dump())
        with self.database.transaction(actor.tenant_id) as c:
            r.authorize(c, actor, "write")
            r.put_resource(c, actor.tenant_id, "application", model)
        return model

    def create_connection(self, actor: Principal, request: ConnectionSubmission) -> Connection:
        binding = self.credential_bindings.get(request.credential_ref)
        with self.database.transaction(actor.tenant_id) as c:
            r.authorize(c, actor, "admin")
            if not binding or binding.tenant_id != actor.tenant_id or binding.host != request.host:
                raise Forbidden()
            if request.host != "https://api.fabric.microsoft.com":
                self._reserve_namespace(c, actor.tenant_id, request.host)
            model = Connection(tenant_id=actor.tenant_id, **request.model_dump())
            r.put_resource(c, actor.tenant_id, "connection", model)
        return model

    @staticmethod
    def _reserve_namespace(c, tenant_id, namespace):
        c.execute(
            insert(m.managed_namespaces)
            .values(host=namespace, tenant_id=tenant_id)
            .on_conflict_do_nothing()
        )
        if not c.execute(
            sa.select(m.managed_namespaces).where(m.managed_namespaces.c.host == namespace)
        ).first():
            raise ServiceError("NAMESPACE_CONFLICT")

    def _reserve_fabric_workspace(self, c, tenant_id, target):
        if target.host == "https://api.fabric.microsoft.com":
            try:
                workspace = str(UUID(target.bindings["workspace"]))
            except (ValueError, KeyError):
                raise ServiceError("FABRIC_WORKSPACE_BINDING_REQUIRED", 422) from None
            self._reserve_namespace(c, tenant_id, target.host + "/workspaces/" + workspace)

    def create_target(self, actor: Principal, request: TargetSubmission) -> Target:
        with self.database.transaction(actor.tenant_id) as c:
            r.authorize(c, actor, "write")
            conn = Connection.model_validate(
                r.resource(c, actor.tenant_id, "connection", request.connection_id)["data"]
            )
            model = Target(
                tenant_id=actor.tenant_id,
                connection_id=conn.id,
                connection_revision=conn.revision,
                host=conn.host,
                environment=request.environment,
                bindings=request.bindings,
            )
            self._reserve_fabric_workspace(c, actor.tenant_id, model)
            r.put_resource(c, actor.tenant_id, "target", model, links={"connection": conn.id})
        return model

    def target(self, c, tenant_id: UUID, identity: UUID) -> Target:
        model = Target.model_validate(r.resource(c, tenant_id, "target", identity)["data"])
        conn = Connection.model_validate(
            r.resource(c, tenant_id, "connection", model.connection_id)["data"]
        )
        return model.model_copy(update={"host": conn.host, "connection_revision": conn.revision})

    def requires_approval(self, c, tenant_id: UUID, target: Target) -> bool:
        connection = Connection.model_validate(
            r.resource(c, tenant_id, "connection", target.connection_id)["data"]
        )
        binding = self.credential_bindings.get(connection.credential_ref)
        if not binding or binding.tenant_id != tenant_id or binding.host != target.host:
            raise Forbidden()
        return binding.require_approval or target.environment in self.require_approval_environments

    def patch_target(
        self, actor: Principal, identity: UUID, patch: TargetPatch, *, expected_revision: int
    ):
        with self.database.transaction(actor.tenant_id) as c:
            r.authorize(c, actor, "write")
            row = r.resource(c, actor.tenant_id, "target", identity, lock=True)
            if row["revision"] != expected_revision:
                raise ServiceError("REVISION_CONFLICT")
            if c.execute(
                sa.select(m.application_claims).where(m.application_claims.c.target_id == identity)
            ).first():
                raise ServiceError("UNRESOLVED_DEPLOYMENT")
            updated = self.target(c, actor.tenant_id, identity).model_copy(
                update=patch.model_dump(exclude_none=True)
            )
            self._reserve_fabric_workspace(c, actor.tenant_id, updated)
            r.replace_resource(c, actor.tenant_id, identity, updated)
            return updated, expected_revision + 1

    def get_resource(self, actor: Principal, kind: str, identity: UUID):
        if kind not in MODEL_KINDS:
            raise NotFound()
        with self.database.transaction(actor.tenant_id) as c:
            r.authorize(c, actor, "read")
            row = r.resource(c, actor.tenant_id, kind, identity)
            model = (
                self.target(c, actor.tenant_id, identity)
                if kind == "target"
                else MODEL_KINDS[kind].model_validate(row["data"])
            )
            return model, row["revision"]

    def create_project(self, actor: Principal, request: ProjectSubmission) -> Project:
        with self.database.transaction(actor.tenant_id) as c:
            r.authorize(c, actor, "write")
            app = Application.model_validate(
                r.resource(c, actor.tenant_id, "application", request.application_id, lock=True)[
                    "data"
                ]
            )
            if app.kind != request.spec.kind:
                raise ServiceError("APPLICATION_KIND_MISMATCH", 422)
            source = SourceArchive.model_validate(
                r.resource(c, actor.tenant_id, "source", request.source_id)["data"]
            )
            revision = (
                c.scalar(
                    sa.select(sa.func.count())
                    .select_from(m.resource_links)
                    .where(
                        m.resource_links.c.child_id == app.id,
                        m.resource_links.c.relation == "application",
                        m.resource_links.c.parent_id.in_(
                            sa.select(m.resources.c.id).where(m.resources.c.kind == "project")
                        ),
                    )
                )
                + 1
            )
            model = Project(
                tenant_id=actor.tenant_id,
                application_id=app.id,
                source=source,
                spec=request.spec,
                revision=revision,
            )
            r.put_resource(
                c,
                actor.tenant_id,
                "project",
                model,
                links={"application": app.id, "source": source.id},
            )
        return model

    def create_upload(self, actor: Principal, request: SourceUploadSubmission) -> SourceUpload:
        model = SourceUpload(
            tenant_id=actor.tenant_id,
            **request.model_dump(),
            expires_at=utcnow() + timedelta(minutes=10),
        )
        with self.database.transaction(actor.tenant_id) as c:
            r.authorize(c, actor, "write")
            r.put_resource(c, actor.tenant_id, "upload", model)
        return model

    def upload_content(self, actor: Principal, identity: UUID, data: bytes):
        with self.database.transaction(actor.tenant_id) as c:
            r.authorize(c, actor, "write")
            upload = SourceUpload.model_validate(
                r.resource(c, actor.tenant_id, "upload", identity, lock=True)["data"]
            )
            if upload.expires_at <= utcnow():
                raise ServiceError("UPLOAD_EXPIRED")
            if len(data) != upload.size or hashlib.sha256(data).hexdigest() != upload.digest:
                raise ServiceError("SOURCE_MISMATCH", 422)
            if upload.source is None:
                try:
                    reference = self.artifacts.put(actor.tenant_id, data)
                except (ValueError, zipfile.BadZipFile):
                    raise ServiceError("INVALID_ARCHIVE", 422) from None
                r.replace_resource(
                    c, actor.tenant_id, identity, upload.model_copy(update={"source": reference})
                )

    def complete_upload(self, actor: Principal, identity: UUID) -> SourceArchive:
        with self.database.transaction(actor.tenant_id) as c:
            r.authorize(c, actor, "write")
            upload = SourceUpload.model_validate(
                r.resource(c, actor.tenant_id, "upload", identity, lock=True)["data"]
            )
            if upload.source is None:
                raise ServiceError("UPLOAD_INCOMPLETE")
            if not c.execute(
                sa.select(m.resources.c.id).where(m.resources.c.id == upload.source.id)
            ).first():
                if upload.expires_at <= utcnow():
                    raise ServiceError("UPLOAD_EXPIRED")
                r.put_resource(c, actor.tenant_id, "source", upload.source)
            return upload.source

    def source_content(self, actor: Principal, identity: UUID) -> bytes:
        source, _ = self.get_resource(actor, "source", identity)
        return self.artifacts.read(source, actor.tenant_id)

    def _inputs(self, c, actor: Principal, kind: str, body: BaseModel) -> dict[str, UUID]:
        tenant = actor.tenant_id
        refs: dict[str, UUID] = {}
        if isinstance(body, (BuildSubmission, ValidationSubmission)):
            project = Project.model_validate(
                r.resource(c, tenant, "project", body.project_id)["data"]
            )
            refs.update(project=project.id, application=project.application_id)
            app = Application.model_validate(
                r.resource(c, tenant, "application", project.application_id)["data"]
            )
        elif isinstance(body, (ScaffoldSubmission, InspectionSubmission)):
            app = Application.model_validate(
                r.resource(c, tenant, "application", body.application_id)["data"]
            )
            refs["application"] = app.id
            if isinstance(body, ScaffoldSubmission) and app.kind != body.spec.kind:
                raise ServiceError("APPLICATION_KIND_MISMATCH", 422)
        else:
            assert isinstance(body, (PlanSubmission, DeploymentSubmission))
            artifact = BuildArtifact.model_validate(
                r.resource(c, tenant, "artifact", body.artifact_id)["data"]
            )
            refs.update(artifact=artifact.id, application=artifact.application_id)
            app = Application.model_validate(
                r.resource(c, tenant, "application", artifact.application_id)["data"]
            )
        if app.kind not in TERMINAL_STEPS:
            raise ServiceError("UNSUPPORTED_APPLICATION_KIND", 422)
        if isinstance(body, InspectionSubmission) and app.kind in {
            "snowflake.native-app/v1",
            "fabric.solution/v1",
            "fabric.iq/v1",
            "palantir.marketplace/v1",
        }:
            latest = c.execute(
                sa.select(m.resources.c.data)
                .join(
                    m.resource_links,
                    sa.and_(
                        m.resource_links.c.parent_id == m.resources.c.id,
                        m.resource_links.c.tenant_id == m.resources.c.tenant_id,
                    ),
                )
                .where(
                    m.resources.c.kind == "project",
                    m.resource_links.c.relation == "application",
                    m.resource_links.c.child_id == app.id,
                )
                .order_by(m.resources.c.data["revision"].as_integer().desc())
                .limit(1)
            ).scalar()
            if not latest:
                raise ServiceError("PROJECT_REQUIRED", 422)
            refs["project"] = Project.model_validate(latest).id
        if isinstance(body, (InspectionSubmission, PlanSubmission)):
            refs["target"] = self.target(c, tenant, body.target_id).id
        if isinstance(body, DeploymentSubmission):
            plan = DeploymentPlan.model_validate(
                r.resource(c, tenant, "plan", body.plan_id)["data"]
            )
            target = self.target(c, tenant, plan.target_id)
            validate_plan(plan, target)
            if plan.artifact_id != artifact.id or plan.artifact_digest != artifact.archive.digest:
                raise ServiceError("PLAN_ARTIFACT_MISMATCH")
            refs.update(plan=plan.id, target=target.id)
        if (
            kind in ("plan", "deployment")
            and "target" in refs
            and c.execute(
                sa.select(m.application_claims).where(
                    m.application_claims.c.application_id == app.id,
                    m.application_claims.c.target_id == refs["target"],
                )
            ).first()
        ):
            raise ServiceError("UNRESOLVED_DEPLOYMENT")
        return refs

    def submit(
        self, actor: Principal, kind: str, request: BaseModel, idempotency_key: str
    ) -> Operation:
        if kind not in SUBMISSIONS or not 1 <= len(idempotency_key) <= 200:
            raise ServiceError("INVALID_SUBMISSION", 422)
        body = SUBMISSIONS[kind].model_validate(request)
        fingerprint = digest(body)
        action = "deploy" if kind == "deployment" else "read" if kind == "inspection" else "write"
        try:
            with self.database.transaction(actor.tenant_id) as c:
                r.authorize(c, actor, action)
                # Serialize identical keys before either request creates an operation.
                lock_number = int(digest([str(actor.tenant_id), kind, idempotency_key])[:15], 16)
                c.execute(sa.text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_number})
                previous = (
                    c.execute(
                        sa.select(m.idempotency_keys).where(
                            m.idempotency_keys.c.route == kind,
                            m.idempotency_keys.c.key == idempotency_key,
                        )
                    )
                    .mappings()
                    .first()
                )
                if previous and previous["expires_at"] > utcnow():
                    if previous["body_digest"] != fingerprint:
                        raise ServiceError("IDEMPOTENCY_CONFLICT")
                    return r.operation_model(
                        r.operation(c, actor.tenant_id, previous["operation_id"])
                    )
                if previous:
                    c.execute(
                        m.idempotency_keys.delete().where(
                            m.idempotency_keys.c.route == kind,
                            m.idempotency_keys.c.key == idempotency_key,
                        )
                    )
                refs = self._inputs(c, actor, kind, body)
                state = "queued"
                if kind == "deployment" and self.requires_approval(
                    c, actor.tenant_id, self.target(c, actor.tenant_id, refs["target"])
                ):
                    state = "waiting_for_policy"
                op = Operation.model_validate(
                    dict(tenant_id=actor.tenant_id, operation_kind=kind, state=state)
                )
                c.execute(
                    m.operations.insert().values(
                        tenant_id=actor.tenant_id,
                        id=op.id,
                        kind=kind,
                        state=state,
                        request=body.model_dump(mode="json"),
                        requester=actor.subject,
                        scopes=sorted(actor.scopes),
                        created_at=op.created_at,
                        deadline=utcnow() + timedelta(seconds=self.operation_timeout_seconds),
                    )
                )
                for relation, child in refs.items():
                    c.execute(
                        m.operation_links.insert().values(
                            tenant_id=actor.tenant_id,
                            operation_id=op.id,
                            relation=relation,
                            resource_id=child,
                        )
                    )
                if kind == "deployment":
                    artifact = BuildArtifact.model_validate(
                        r.resource(c, actor.tenant_id, "artifact", refs["artifact"])["data"]
                    )
                    deployment = Deployment(
                        id=op.id,
                        tenant_id=actor.tenant_id,
                        application_id=refs["application"],
                        target_id=refs["target"],
                        artifact_digest=artifact.archive.digest,
                        resources=[],
                        healthy=False,
                    )
                    r.put_resource(c, actor.tenant_id, "deployment", deployment, links=refs)
                c.execute(
                    m.idempotency_keys.insert().values(
                        tenant_id=actor.tenant_id,
                        route=kind,
                        key=idempotency_key,
                        body_digest=fingerprint,
                        operation_id=op.id,
                        expires_at=utcnow() + timedelta(days=7),
                    )
                )
                r.event(c, actor.tenant_id, op.id, "submitted", {"actor": actor.subject})
                r.enqueue(c, actor.tenant_id, op.id)
                return op
        except sa.exc.IntegrityError:
            raise ServiceError("RESOURCE_CONFLICT") from None
        except ValueError:
            raise ServiceError("INVALID_SUBMISSION", 422) from None

    def get_operation(self, actor: Principal, identity: UUID) -> Operation:
        with self.database.transaction(actor.tenant_id) as c:
            r.authorize(c, actor, "read")
            return r.operation_model(r.operation(c, actor.tenant_id, identity))

    def events(
        self, actor: Principal, identity: UUID, *, after: int = 0, limit: int = 100
    ) -> EventPage:
        if after < 0 or not 1 <= limit <= 200:
            raise ServiceError("INVALID_CURSOR", 422)
        with self.database.transaction(actor.tenant_id) as c:
            r.authorize(c, actor, "read")
            r.operation(c, actor.tenant_id, identity)
            rows = (
                c.execute(
                    sa.select(m.events)
                    .where(m.events.c.operation_id == identity, m.events.c.seq > after)
                    .order_by(m.events.c.seq)
                    .limit(limit)
                )
                .mappings()
                .all()
            )
            data = [
                OperationEvent(
                    seq=row["seq"], name=row["name"], data=row["data"], created_at=row["created_at"]
                )
                for row in rows
            ]
            return EventPage(data=data, next_cursor=data[-1].seq if data else after)

    def deployment(self, actor: Principal, identity: UUID) -> DeploymentView:
        model, _ = self.get_resource(actor, "deployment", identity)
        return DeploymentView(deployment=model, operation=self.get_operation(actor, identity))

    def capabilities(self, actor: Principal, target_id: UUID) -> CapabilityReport:
        with self.database.transaction(actor.tenant_id) as c:
            r.authorize(c, actor, "read")
            self.target(c, actor.tenant_id, target_id)
            try:
                report = CapabilityReport.model_validate(
                    r.resource(
                        c, actor.tenant_id, "capabilities", uuid5(target_id, "capabilities")
                    )["data"]
                )
                if report.expires_at > utcnow():
                    return report
            except NotFound:
                pass
            return CapabilityReport(
                adapter_version="0.1.0",
                application_kinds=["databricks.app/v1"],
                capabilities=[
                    Capability(
                        operation="workspace",
                        status="unknown",
                        reason="submit an inspection to refresh capabilities",
                    )
                ],
                expires_at=utcnow() + timedelta(minutes=1),
            )

    def platform_schema(self, actor: Principal, platform: str):
        from typing import Any, cast

        from pydantic import TypeAdapter
        from shifde_contracts.projects import ApplicationSpec

        with self.database.transaction(actor.tenant_id) as c:
            r.authorize(c, actor, "read")
        mapping = {
            "databricks": "DatabricksApplicationSpec",
            "palantir": "FoundryApplicationSpec",
            "snowflake": "SnowflakeApplicationSpec",
            "fabric": "FabricApplicationSpec",
            "fabric-iq": "FabricIQApplicationSpec",
            "palantir-marketplace": "FoundryMarketplaceSpec",
        }
        if platform not in mapping:
            raise ServiceError("UNSUPPORTED_PLATFORM", 404)
        schema = TypeAdapter[Any](cast(Any, ApplicationSpec)).json_schema()
        return {"$ref": "#/$defs/" + mapping[platform], "$defs": schema["$defs"]}

    def cancel(self, actor: Principal, identity: UUID) -> Operation:
        with self.database.transaction(actor.tenant_id) as c:
            row = r.operation(c, actor.tenant_id, identity, lock=True)
            r.authorize(
                c,
                actor,
                "admin"
                if row["requester"] != actor.subject
                else "deploy"
                if row["kind"] == "deployment"
                else "write",
            )
            if row["state"] in r.TERMINAL:
                raise ServiceError("OPERATION_TERMINAL")
            c.execute(
                m.operations.update()
                .where(m.operations.c.id == identity)
                .values(cancel_requested=True)
            )
            r.event(c, actor.tenant_id, identity, "cancellation_requested")
            if row["state"] in ("queued", "waiting_for_policy"):
                r.set_state(c, row, "cancelled")
            r.enqueue(c, actor.tenant_id, identity, "wake")
            return r.operation_model(r.operation(c, actor.tenant_id, identity))

    def resume(self, actor: Principal, identity: UUID) -> Operation:
        with self.database.transaction(actor.tenant_id) as c:
            row = r.operation(c, actor.tenant_id, identity, lock=True)
            r.authorize(c, actor, "deploy")
            if row["kind"] != "deployment" or row["state"] != "needs_attention":
                raise ServiceError("UNPROVEN_NATIVE_OUTCOME")
            artifact = BuildArtifact.model_validate(
                r.resource(c, actor.tenant_id, "artifact", UUID(row["request"]["artifact_id"]))[
                    "data"
                ]
            )
            completed = c.execute(
                sa.select(m.step_journal).where(
                    m.step_journal.c.operation_id == identity,
                    m.step_journal.c.key == terminal_step(artifact.spec.kind),
                    m.step_journal.c.state == "complete",
                )
            ).first()
            if row["state"] != "needs_attention" or not completed:
                raise ServiceError("UNPROVEN_NATIVE_OUTCOME")
            c.execute(
                m.operations.update()
                .where(m.operations.c.id == identity)
                .values(
                    requester=actor.subject,
                    scopes=sorted(actor.scopes),
                    deadline=utcnow() + timedelta(seconds=self.operation_timeout_seconds),
                )
            )
            r.event(
                c,
                actor.tenant_id,
                identity,
                "recovery_delegated",
                {
                    "previous_actor": row["requester"],
                    "actor": actor.subject,
                },
            )
            r.set_state(c, row, "reconciling")
            r.enqueue(c, actor.tenant_id, identity, "resume:" + str(uuid4()))
            return r.operation_model(r.operation(c, actor.tenant_id, identity))

    def decide_policy(self, actor: Principal, request: PolicyDecisionSubmission) -> Operation:
        with self.database.transaction(actor.tenant_id) as c:
            r.authorize(c, actor, "approve")
            row = r.operation(c, actor.tenant_id, request.operation_id, lock=True)
            if row["state"] != "waiting_for_policy" or row["requester"] == actor.subject:
                raise ServiceError("INVALID_POLICY_DECISION")
            if c.execute(
                sa.select(m.resources.c.id).where(
                    m.resources.c.kind == "policy_decision",
                    m.resources.c.data["operation_id"].as_string() == str(row["id"]),
                )
            ).first():
                raise ServiceError("POLICY_ALREADY_DECIDED")
            plan = DeploymentPlan.model_validate(
                r.resource(c, actor.tenant_id, "plan", UUID(row["request"]["plan_id"]))["data"]
            )
            if plan.digest != request.plan_digest or plan.expires_at <= utcnow():
                raise ServiceError("STALE_POLICY_DECISION")
            decision = {**request.model_dump(mode="json"), "actor": actor.subject}
            c.execute(
                m.resources.insert().values(
                    tenant_id=actor.tenant_id, id=uuid4(), kind="policy_decision", data=decision
                )
            )
            r.event(
                c,
                actor.tenant_id,
                row["id"],
                "policy_approved" if request.approve else "policy_denied",
            )
            if not request.approve:
                r.set_state(
                    c,
                    row,
                    "failed",
                    error=Diagnostic(
                        code="POLICY_DENIED", message="deployment policy denied the operation"
                    ),
                )
            r.enqueue(c, actor.tenant_id, row["id"], "wake")
            return r.operation_model(r.operation(c, actor.tenant_id, row["id"]))

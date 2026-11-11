"""Durable execution authority. Workers cannot supply their own jobs or credentials."""

import json
import re
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

import sqlalchemy as sa
from shifde_contracts.checkpoints import NativeStepReceipt
from shifde_contracts.common import SourceArchive, utcnow
from shifde_contracts.execution import ApplyReceipt, Deployment
from shifde_contracts.hosted import Connection
from shifde_contracts.jobs import (
    CheckpointSubmission,
    CredentialGrant,
    ExecutionJob,
    JobLease,
    JobResultSubmission,
    LeaseRequest,
    WorkerPool,
)
from shifde_contracts.projects import parse_spec
from shifde_core.fabric_receipts import validate_fabric_receipt
from shifde_core.hashing import digest
from shifde_core.native import terminal_step
from sqlalchemy.dialects.postgresql import insert

from shifde_hosted.auth import Principal
from shifde_hosted.errors import Forbidden, ServiceError
from shifde_hosted.job_types import OFFLINE, RESULTS, input_archives
from shifde_hosted.journal import PostgresJournal
from shifde_hosted.persistence import models as m
from shifde_hosted.persistence import repository as r


@dataclass(frozen=True)
class WorkerPrincipal:
    tenant_id: UUID
    pool_id: UUID
    worker_id: UUID
    subject: str


class ExecutionGateway:
    def __init__(self, service, *, lease_seconds: int = 45):
        self.service, self.database, self.lease_seconds = service, service.database, lease_seconds

    def _pool(self, c, tenant, identity):
        row = c.execute(
            sa.select(m.worker_pools.c.data).where(
                m.worker_pools.c.tenant_id == tenant, m.worker_pools.c.id == identity
            )
        ).scalar()
        if not row:
            raise Forbidden()
        pool = WorkerPool.model_validate(row)
        if not pool.enabled:
            raise Forbidden()
        return pool

    def _worker(self, c, worker):
        row = c.execute(
            sa.select(m.executors).where(
                m.executors.c.tenant_id == worker.tenant_id,
                m.executors.c.id == worker.worker_id,
                m.executors.c.pool_id == worker.pool_id,
                m.executors.c.subject == worker.subject,
                m.executors.c.enabled.is_(True),
            )
        ).first()
        enabled = c.scalar(sa.select(m.tenants.c.enabled).where(m.tenants.c.id == worker.tenant_id))
        if not row or not enabled:
            raise Forbidden()
        return self._pool(c, worker.tenant_id, worker.pool_id)

    def enqueue(self, action, request, ctx):
        tenant = ctx.tenant_id
        body = request.model_dump(mode="json") if request is not None else {}
        target = getattr(ctx, "target", None)
        with self.database.transaction(tenant) as c:
            op = r.operation(c, tenant, ctx.operation_id, lock=True)
            if op["state"] != "running" or op["deadline"] <= utcnow():
                raise ServiceError("OPERATION_NOT_RUNNING")
            application_id = c.scalar(
                sa.select(m.operation_links.c.resource_id).where(
                    m.operation_links.c.operation_id == op["id"],
                    m.operation_links.c.relation == "application",
                )
            )
            application_kind = r.resource(c, tenant, "application", application_id)["data"]["kind"]
            if action in {"observe", "verify"} and not application_kind.startswith("fabric."):
                # Older pinned Databricks/Snowflake runners accept the receipt-only wire shape.
                body.pop("spec", None)
            pools = [
                WorkerPool.model_validate(row)
                for row in c.scalars(sa.select(m.worker_pools.c.data))
            ]
            selected = [
                p
                for p in pools
                if p.enabled
                and action in p.actions
                and application_kind in p.application_kinds
                and (target is None or target.connection_id in p.connection_ids)
            ]
            if len(selected) != 1:
                raise ServiceError("EXECUTOR_POOL_AMBIGUOUS_OR_MISSING")
            pool = selected[0]
            credential_ref = None
            if target:
                conn = Connection.model_validate(
                    r.resource(c, tenant, "connection", target.connection_id)["data"]
                )
                binding = self.service.credential_bindings.get(conn.credential_ref)
                if not binding or binding.tenant_id != tenant or binding.host != target.host:
                    raise Forbidden()
                if binding.execution_pool_id and binding.execution_pool_id != pool.id:
                    raise Forbidden()
                credential_ref = conn.credential_ref
            checkpoints = {
                row.key: {"state": row.state, "receipt": row.receipt}
                for row in c.execute(
                    sa.select(m.step_journal).where(m.step_journal.c.operation_id == op["id"])
                )
            }
            job = ExecutionJob(
                tenant_id=tenant,
                operation_id=op["id"],
                application_kind=application_kind,
                execution_id=op["execution_id"],
                operation_fence=op["fence"],
                pool_id=pool.id,
                action=action,
                image=pool.image,
                request=body,
                target=target,
                owned_resources=list(getattr(ctx, "owned_resources", ())),
                inputs=input_archives(body),
                toolchain_digest=getattr(ctx, "toolchain_digest", "0" * 64),
                deadline=op["deadline"],
                credential_ref=credential_ref,
                journal=checkpoints,
            )
            if any(ref.tenant_id != tenant for ref in job.inputs):
                raise Forbidden()
            c.execute(
                m.execution_jobs.insert().values(
                    tenant_id=tenant,
                    id=job.id,
                    operation_id=job.operation_id,
                    pool_id=pool.id,
                    action=action,
                    data=job.model_dump(mode="json"),
                )
            )
            r.event(
                c,
                tenant,
                op["id"],
                "executor_job_queued",
                {"job_id": str(job.id), "action": action},
            )
        return job

    def _owned(self, c, worker, identity, proof, *, writing=False, terminal=False):
        pool = self._worker(c, worker)
        initial = (
            c.execute(sa.select(m.execution_jobs).where(m.execution_jobs.c.id == identity))
            .mappings()
            .first()
        )
        if not initial:
            raise Forbidden()
        op = r.operation(c, worker.tenant_id, initial["operation_id"], lock=True)
        row = (
            c.execute(
                sa.select(m.execution_jobs)
                .where(m.execution_jobs.c.id == identity)
                .with_for_update()
            )
            .mappings()
            .one()
        )
        job = ExecutionJob.model_validate(row["data"])
        if job.target:
            binding = self.service.credential_bindings.get(job.credential_ref or "")
            if (
                job.target.connection_id not in pool.connection_ids
                or not binding
                or binding.tenant_id != worker.tenant_id
                or binding.host != job.target.host
                or binding.execution_pool_id is not None
                and binding.execution_pool_id != pool.id
            ):
                raise Forbidden()
        if (
            row["pool_id"] != worker.pool_id
            or row["worker_id"] != worker.worker_id
            or row["lease_id"] != proof.lease_id
            or row["fence"] != proof.fence
            or job.image != pool.image
            or job.action not in pool.actions
            or job.application_kind not in pool.application_kinds
        ):
            raise Forbidden()
        now = c.scalar(sa.select(sa.func.now()))
        if (
            row["lease_until"] <= now
            or job.deadline <= now
            or row["state"] not in ({"leased", "succeeded", "failed"} if terminal else {"leased"})
        ):
            raise ServiceError("LEASE_EXPIRED")
        if (
            op["execution_id"] != job.execution_id
            or op["fence"] != job.operation_fence
            or (
                op["state"] != "running"
                and not (terminal and row["state"] in {"succeeded", "failed"})
            )
        ):
            raise ServiceError("STALE_EXECUTION")
        action = (
            "deploy"
            if op["kind"] == "deployment"
            else "read"
            if op["kind"] == "inspection"
            else "write"
        )
        r.authorize(
            c, Principal(worker.tenant_id, op["requester"], frozenset(op["scopes"])), action
        )
        if writing and op["cancel_requested"]:
            raise ServiceError("CANCELLATION_REQUESTED")
        return row, job, op

    def lease(self, worker: WorkerPrincipal, request: LeaseRequest) -> JobLease | None:
        with self.database.transaction(worker.tenant_id) as c:
            pool = self._worker(c, worker)
            if request.image != pool.image:
                raise Forbidden()
            # Serialize polls for one worker identity before choosing a queued job.
            c.execute(
                sa.select(m.executors.c.id)
                .where(m.executors.c.id == worker.worker_id)
                .with_for_update()
            ).one()
            active = (
                c.execute(
                    sa.select(m.execution_jobs)
                    .where(
                        m.execution_jobs.c.pool_id == pool.id,
                        m.execution_jobs.c.worker_id == worker.worker_id,
                        m.execution_jobs.c.state == "leased",
                        m.execution_jobs.c.lease_until > sa.func.now(),
                    )
                    .with_for_update()
                    .limit(1)
                )
                .mappings()
                .first()
            )
            if active:
                if active["lease_request_id"] != request.request_id:
                    return None
                return JobLease(
                    job=ExecutionJob.model_validate(active["data"]),
                    lease_id=active["lease_id"],
                    fence=active["fence"],
                    expires_at=active["lease_until"],
                    request_id=request.request_id,
                )
            # No expired lease is ever eligible for replacement.
            row = (
                c.execute(
                    sa.select(m.execution_jobs)
                    .where(
                        m.execution_jobs.c.pool_id == pool.id, m.execution_jobs.c.state == "queued"
                    )
                    .order_by(m.execution_jobs.c.created_at)
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
                .mappings()
                .first()
            )
            if not row:
                return None
            job = ExecutionJob.model_validate(row["data"])
            if (
                job.image != pool.image
                or job.action not in pool.actions
                or job.application_kind not in pool.application_kinds
                or job.deadline <= utcnow()
            ):
                c.execute(
                    m.execution_jobs.update()
                    .where(m.execution_jobs.c.id == row["id"])
                    .values(state="lost")
                )
                return None
            lease_id, fence = uuid4(), row["fence"] + 1
            expires = min(
                job.deadline,
                c.scalar(sa.select(sa.func.now())) + timedelta(seconds=self.lease_seconds),
            )
            c.execute(
                m.execution_jobs.update()
                .where(m.execution_jobs.c.id == job.id)
                .values(
                    state="leased",
                    worker_id=worker.worker_id,
                    lease_id=lease_id,
                    lease_request_id=request.request_id,
                    fence=fence,
                    lease_until=expires,
                )
            )
            return JobLease(
                job=job,
                lease_id=lease_id,
                fence=fence,
                expires_at=expires,
                request_id=request.request_id,
            )

    def heartbeat(self, worker, identity, proof):
        with self.database.transaction(worker.tenant_id) as c:
            row, job, op = self._owned(c, worker, identity, proof)
            expires = min(
                job.deadline,
                c.scalar(sa.select(sa.func.now())) + timedelta(seconds=self.lease_seconds),
            )
            c.execute(
                m.execution_jobs.update()
                .where(m.execution_jobs.c.id == identity)
                .values(lease_until=expires)
            )
            return {
                "expires_at": expires.isoformat(),
                "cancel_requested": bool(op["cancel_requested"] and job.action == "apply"),
            }

    def guard(self, worker, identity, proof):
        with self.database.transaction(worker.tenant_id) as c:
            self._owned(c, worker, identity, proof, writing=True)
        return {"allowed": True}

    def credential_grant(self, worker, identity, proof):
        with self.database.transaction(worker.tenant_id) as c:
            row, job, op = self._owned(c, worker, identity, proof)
            if job.action == "apply" and op["cancel_requested"]:
                raise ServiceError("CANCELLATION_REQUESTED")
            if job.action in OFFLINE or not job.target or not job.credential_ref:
                raise Forbidden()
            return CredentialGrant(
                tenant_id=worker.tenant_id,
                job_id=job.id,
                worker_id=worker.worker_id,
                pool_id=worker.pool_id,
                target_id=job.target.id,
                credential_ref=job.credential_ref,
                audience=job.target.host,
                expires_at=min(row["lease_until"], utcnow() + timedelta(seconds=30)),
            )

    def checkpoint(self, worker, identity, request: CheckpointSubmission):
        if len(json.dumps(request.data)) > 16384:
            raise ServiceError("CHECKPOINT_TOO_LARGE", 413)
        with self.database.transaction(worker.tenant_id) as c:
            row, job, op = self._owned(
                c, worker, identity, request, writing=request.phase == "intent"
            )
            if job.action != "apply":
                raise Forbidden()
            previous = (
                c.execute(
                    sa.select(m.execution_checkpoints).where(
                        m.execution_checkpoints.c.job_id == identity,
                        m.execution_checkpoints.c.sequence == request.sequence,
                    )
                )
                .mappings()
                .first()
            )
            fingerprint = digest(request)
            if previous:
                if previous["digest"] != fingerprint:
                    raise ServiceError("CHECKPOINT_CONFLICT")
                return previous["response"]
            if request.sequence != row["sequence"] + 1:
                raise ServiceError("CHECKPOINT_SEQUENCE_CONFLICT")
            journal = PostgresJournal(
                self.database,
                worker.tenant_id,
                op["id"],
                job.execution_id,
                job.operation_fence,
                connection=c,
            )
            if request.phase == "intent":
                steps = job.request["plan"]["steps"]
                keys = [step["key"] for step in steps]
                if request.step not in keys or any(
                    journal.completed(k) is None for k in keys[: keys.index(request.step)]
                ):
                    raise ServiceError("UNPLANNED_NATIVE_STEP")
                accepted = journal.intent(request.step, request.data)
                response = {"accepted": accepted}
            else:
                if job.application_kind in {
                    "snowflake.native-app/v1",
                    "fabric.solution/v1",
                    "fabric.iq/v1",
                } and request.step != terminal_step(job.application_kind):
                    step_receipt = NativeStepReceipt.model_validate(request.data)
                    fabric = job.application_kind.startswith("fabric.")
                    if set(step_receipt.handles) - (
                        {"operation_id", "job_id", "metadata_digest", "definition_digest"}
                        if fabric
                        else {"query_id"}
                    ) or any(
                        value and not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value)
                        for value in step_receipt.handles.values()
                    ):
                        raise ServiceError("INVALID_NATIVE_RECEIPT", 422)
                    if (
                        step_receipt.step_key != request.step
                        or step_receipt.plan_digest != job.request["plan"]["digest"]
                    ):
                        raise ServiceError("CHECKPOINT_CONFLICT")
                    allowed = {s["resource_key"] for s in job.request["plan"]["steps"]}
                    for binding in step_receipt.resources:
                        if (
                            str(binding.application_id) != job.request["artifact"]["application_id"]
                            or not job.target
                            or binding.target_id != job.target.id
                            or binding.key not in allowed
                            or not binding.resource_type.startswith(
                                "fabric." if fabric else "snowflake."
                            )
                        ):
                            raise Forbidden()
                        values = dict(
                            tenant_id=worker.tenant_id,
                            application_id=binding.application_id,
                            target_id=binding.target_id,
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
                                    "native_id": binding.native_id,
                                    "resource_type": binding.resource_type,
                                    "data": values["data"],
                                },
                            )
                        )
                    deployment = Deployment.model_validate(
                        r.resource(c, worker.tenant_id, "deployment", job.operation_id)["data"]
                    )
                    merged = {b.key: b for b in deployment.resources}
                    merged.update({b.key: b for b in step_receipt.resources})
                    r.replace_resource(
                        c,
                        worker.tenant_id,
                        job.operation_id,
                        deployment.model_copy(update={"resources": list(merged.values())}),
                    )
                    if step_receipt.handles.get("query_id"):
                        r.event(
                            c,
                            worker.tenant_id,
                            job.operation_id,
                            "native_query_recorded",
                            {"step": request.step, "query_id": step_receipt.handles["query_id"]},
                        )
                    if fabric:
                        for handle in ("operation_id", "job_id"):
                            if step_receipt.handles.get(handle):
                                r.event(
                                    c,
                                    worker.tenant_id,
                                    job.operation_id,
                                    "native_operation_recorded",
                                    {"step": request.step, handle: step_receipt.handles[handle]},
                                )
                elif request.step == "bundle-deploy":
                    if set(request.data) != {"native_id"} or not re.fullmatch(
                        r"[A-Za-z0-9_.:-]{1,256}", str(request.data["native_id"])
                    ):
                        raise ServiceError("INVALID_NATIVE_RECEIPT", 422)
                elif request.step == terminal_step(job.application_kind):
                    receipt = ApplyReceipt.model_validate(request.data)
                    artifact = job.request["artifact"]
                    if job.application_kind.startswith("fabric."):
                        try:
                            validate_fabric_receipt(
                                receipt,
                                parse_spec(artifact["spec"]),
                                job.target,
                                completed=journal.completed,
                            )
                        except ValueError:
                            raise ServiceError("INVALID_NATIVE_RECEIPT", 422) from None
                    if (
                        receipt.tenant_id != worker.tenant_id
                        or not job.target
                        or receipt.target_id != job.target.id
                        or str(receipt.application_id) != artifact["application_id"]
                        or receipt.artifact_digest != artifact["archive"]["digest"]
                    ):
                        raise Forbidden()
                else:
                    raise ServiceError("UNPLANNED_NATIVE_STEP")
                journal.complete(request.step, request.data)
                response = {"accepted": True}
            c.execute(
                m.execution_checkpoints.insert().values(
                    tenant_id=worker.tenant_id,
                    job_id=identity,
                    sequence=request.sequence,
                    digest=fingerprint,
                    response=response,
                )
            )
            c.execute(
                m.execution_jobs.update()
                .where(m.execution_jobs.c.id == identity)
                .values(sequence=request.sequence)
            )
            return response

    def input_content(self, worker, identity, proof, source_id):
        with self.database.transaction(worker.tenant_id) as c:
            _, job, _ = self._owned(c, worker, identity, proof)
            ref = next((s for s in job.inputs if s.id == source_id), None)
            if ref is None:
                raise Forbidden()
            return self.service.artifacts.read(ref, worker.tenant_id)

    def output_content(self, worker, identity, proof, data):
        with self.database.transaction(worker.tenant_id) as c:
            row, job, _ = self._owned(c, worker, identity, proof)
            if job.action not in {"build", "scaffold"} or len(row["outputs"]) >= 2:
                raise Forbidden()
            ref = self.service.artifacts.put(worker.tenant_id, data)
            c.execute(
                m.execution_jobs.update()
                .where(m.execution_jobs.c.id == identity)
                .values(outputs=[*row["outputs"], ref.model_dump(mode="json")])
            )
            return ref

    def result(self, worker, identity, request: JobResultSubmission):
        with self.database.transaction(worker.tenant_id) as c:
            row, job, _ = self._owned(c, worker, identity, request, terminal=True)
            payload = request.model_dump(mode="json")
            if row["state"] != "leased":
                if row["result"] != payload:
                    raise ServiceError("RESULT_CONFLICT")
                return {"accepted": True}
            if request.status == "succeeded":
                model = RESULTS[job.action].model_validate(request.value)
                if (
                    isinstance(model, SourceArchive)
                    and model.model_dump(mode="json") not in row["outputs"]
                ):
                    raise Forbidden()
                if job.action == "build" and model.model_dump(mode="json")["archive"] not in [
                    *row["outputs"],
                    *(ref.model_dump(mode="json") for ref in job.inputs),
                ]:
                    raise Forbidden()
                if job.action == "apply" and (
                    not isinstance(model, ApplyReceipt)
                    or model.model_dump(mode="json")
                    != c.scalar(
                        sa.select(m.step_journal.c.receipt).where(
                            m.step_journal.c.operation_id == job.operation_id,
                            m.step_journal.c.key == terminal_step(job.application_kind),
                            m.step_journal.c.state == "complete",
                        )
                    )
                ):
                    raise ServiceError("UNPROVEN_NATIVE_OUTCOME")
                payload["value"] = model.model_dump(mode="json")
            else:
                payload["value"] = None
            c.execute(
                m.execution_jobs.update()
                .where(m.execution_jobs.c.id == identity)
                .values(state=request.status, result=payload)
            )
            r.event(
                c,
                worker.tenant_id,
                job.operation_id,
                "executor_job_finished",
                {"job_id": str(identity), "status": request.status},
            )
            return {"accepted": True}

    def outcome(self, job):
        with self.database.transaction(job.tenant_id) as c:
            op = r.operation(c, job.tenant_id, job.operation_id, lock=True)
            row = (
                c.execute(
                    sa.select(m.execution_jobs)
                    .where(m.execution_jobs.c.id == job.id)
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            if (
                row["state"] == "leased"
                and row["lease_until"] <= utcnow()
                or job.deadline <= utcnow()
                and row["state"] in {"queued", "leased"}
            ):
                c.execute(
                    m.execution_jobs.update()
                    .where(m.execution_jobs.c.id == job.id)
                    .values(state="lost")
                )
                return {"status": "lost"}
            if row["state"] in {"succeeded", "failed"}:
                return row["result"]
            if row["state"] == "lost":
                return {"status": "lost"}
            if op["state"] != "running":
                return {"status": "lost"}
            return None

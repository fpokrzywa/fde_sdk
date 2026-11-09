"""Trusted local SDK composition; this is not the future multi-tenant HTTP service."""

import json
from pathlib import Path
from uuid import UUID, uuid5

from filelock import FileLock
from shifde_contracts.builds import BuildArtifact, BuildRequest, ScaffoldRequest
from shifde_contracts.common import ResourceBinding, SourceArchive, Target, VerificationReport
from shifde_contracts.execution import (
    ApplyReceipt,
    ApplyRequest,
    NativeObservation,
    ObserveRequest,
    VerifyRequest,
)
from shifde_contracts.plans import DeploymentPlan, PlanRequest
from shifde_contracts.projects import ApplicationSpec

from shifde_core.adapter import PlatformAdapter
from shifde_core.artifact_store import LocalArtifactStore, atomic_write
from shifde_core.contexts import BuildContext, DeployContext, ReadContext
from shifde_core.hashing import canonical_bytes, digest
from shifde_core.journal import NeedsAttention, StepJournal


class LocalDeliveryService:
    def __init__(
        self,
        root: Path,
        *,
        tenant_id: UUID,
        target: Target,
        adapter: PlatformAdapter,
        toolchain_digest: str,
    ):
        if target.tenant_id != tenant_id:
            raise PermissionError("target belongs to another tenant")
        self.root = root / str(tenant_id)
        self.tenant_id, self.target, self.adapter = tenant_id, target, adapter
        self.artifacts = LocalArtifactStore(self.root / "artifacts")
        self.build_context = BuildContext(
            tenant_id=tenant_id,
            artifacts=self.artifacts,
            workspace=self.root / "build",
            toolchain_digest=toolchain_digest,
        )

    def _ownership_path(self, application_id: UUID) -> Path:
        return self.root / "ownership" / f"{application_id}-{self.target.id}.json"

    def _read_context(self, application_id: UUID) -> ReadContext:
        path = self._ownership_path(application_id)
        resources = (
            tuple(ResourceBinding.model_validate(item) for item in json.loads(path.read_bytes()))
            if path.exists()
            else ()
        )
        return ReadContext(tenant_id=self.tenant_id, target=self.target, owned_resources=resources)

    async def scaffold(self, spec: ApplicationSpec) -> SourceArchive:
        return await self.adapter.scaffold(ScaffoldRequest(spec=spec), self.build_context)

    async def build(
        self, application_id: UUID, spec: ApplicationSpec, source: SourceArchive
    ) -> BuildArtifact:
        artifact = await self.adapter.build(
            BuildRequest(application_id=application_id, spec=spec, source=source),
            self.build_context,
        )
        atomic_write(self.root / "builds" / f"{artifact.id}.json", canonical_bytes(artifact))
        return artifact

    async def plan(self, artifact: BuildArtifact) -> DeploymentPlan:
        if self._claim_path(artifact.application_id).exists():
            raise NeedsAttention(
                "resolve the previous application operation before creating a new plan"
            )
        plan = await self.adapter.plan(
            PlanRequest(artifact=artifact), self._read_context(artifact.application_id)
        )
        atomic_write(self.root / "plans" / f"{plan.id}.json", canonical_bytes(plan))
        return plan

    async def deploy(self, artifact: BuildArtifact, plan: DeploymentPlan) -> ApplyReceipt:
        claim = self._claim_path(artifact.application_id)
        claim.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(claim) + ".lock", timeout=0):
            if claim.exists() and json.loads(claim.read_bytes())["plan_id"] != str(plan.id):
                raise NeedsAttention("another plan has an unresolved application operation")
            atomic_write(claim, canonical_bytes({"plan_id": str(plan.id)}))
            try:
                result = await self._deploy_recorded(artifact, plan)
            except BaseException:
                operation_id = uuid5(plan.id, "apply")
                journal = StepJournal(
                    self.root / "operations" / f"{operation_id}.json", self.tenant_id, operation_id
                )
                if not any(journal.has_intent(step.key) for step in plan.steps):
                    claim.unlink(missing_ok=True)
                raise
            claim.unlink()
            return result

    def _claim_path(self, application_id: UUID) -> Path:
        key = digest([str(application_id), str(self.target.id)])[:32]
        return self.root / "active" / f"{key}.json"

    async def _deploy_recorded(self, artifact: BuildArtifact, plan: DeploymentPlan) -> ApplyReceipt:
        recorded = DeploymentPlan.model_validate_json(
            (self.root / "plans" / f"{plan.id}.json").read_bytes()
        )
        if canonical_bytes(recorded) != canonical_bytes(plan):
            raise ValueError("plan differs from the locally recorded plan")
        operation_id = uuid5(plan.id, "apply")
        context = self._read_context(artifact.application_id)
        journal = StepJournal(
            self.root / "operations" / f"{operation_id}.json", self.tenant_id, operation_id
        )
        deploy_context = DeployContext(
            tenant_id=self.tenant_id,
            target=self.target,
            owned_resources=context.owned_resources,
            operation_id=operation_id,
            artifacts=self.artifacts,
            journal=journal,
            toolchain_digest=self.build_context.toolchain_digest,
        )
        receipt = await self.adapter.apply(
            ApplyRequest(artifact=artifact, plan=plan), deploy_context
        )
        atomic_write(
            self._ownership_path(artifact.application_id),
            canonical_bytes([item.model_dump(mode="json") for item in receipt.resources]),
        )
        atomic_write(self.root / "receipts" / f"{receipt.id}.json", canonical_bytes(receipt))
        return receipt

    async def observe(self, receipt: ApplyReceipt) -> NativeObservation:
        return await self.adapter.observe(
            ObserveRequest(receipt=receipt), self._read_context(receipt.application_id)
        )

    async def verify(self, receipt: ApplyReceipt) -> VerificationReport:
        return await self.adapter.verify(
            VerifyRequest(receipt=receipt), self._read_context(receipt.application_id)
        )

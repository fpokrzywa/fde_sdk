"""Pinned, locally approved native product installation; SuperRepo remains gated."""

from datetime import timedelta

from shifde_contracts.builds import BuildArtifact
from shifde_contracts.capabilities import Capability, CapabilityReport
from shifde_contracts.checkpoints import NativeStepReceipt
from shifde_contracts.common import (
    Check,
    Diagnostic,
    ResourceBinding,
    ValidationReport,
    VerificationReport,
    utcnow,
)
from shifde_contracts.execution import ApplyReceipt, NativeObservation, ReconciliationResult
from shifde_contracts.plans import DeploymentPlan, EnvironmentSnapshot, PlanStep
from shifde_core.artifact_store import create_archive, read_archive
from shifde_core.hashing import digest
from shifde_core.journal import NeedsAttention
from shifde_core.palantir_receipts import job_payload, validate_palantir_receipt
from shifde_core.planning import bindings_digest, seal_plan, validate_plan
from shifde_core.policy import validate_changes

from shifde_palantir.source import (
    ADAPTER_VERSION,
    TERMINAL,
    TOOLCHAIN_DIGEST,
    bindings,
    files_for,
    native_id,
    rid,
    validate_source,
)

BASE = "/api/v3/platform/marketplaceInstallations"
JOBS = "/api/v3/platform/marketplaceInstallationJobs"


class SuperRepoPrerequisite(ValueError):
    pass


class PalantirSuperRepoAdapter:
    kind = "palantir.superrepo/v1"

    async def capabilities(self, ctx):
        return CapabilityReport(
            adapter_version=ADAPTER_VERSION,
            application_kinds=[self.kind],
            capabilities=[
                Capability(
                    operation="deployment",
                    status="unsupported",
                    reason="Requires enrollment SuperRepo enablement and an approved, pinned enrollment CLI",
                )
            ],
            expires_at=utcnow() + timedelta(minutes=5),
        )

    async def _gated(self, request, ctx):
        raise SuperRepoPrerequisite(
            "SuperRepo requires an enabled enrollment and approved enrollment CLI build"
        )

    scaffold = validate = build = inspect = plan = apply = observe = verify = reconcile = _gated


class PalantirMarketplaceAdapter:
    kind = "palantir.marketplace/v1"

    def __init__(self, *, client_factory=None):
        self.client_factory = client_factory

    def _client(self, ctx, spec=None):
        ctx.check_deadline()
        if not self.client_factory:
            raise ValueError("Foundry native credentials unavailable")
        client = self.client_factory(ctx.target)
        client.deadline = ctx.deadline
        client.auth()
        if spec:
            client.approve(spec)
        return client

    async def capabilities(self, ctx):
        return CapabilityReport(
            adapter_version=ADAPTER_VERSION,
            application_kinds=[self.kind],
            capabilities=[
                Capability(
                    operation="deployment",
                    status="unknown",
                    reason="Experimental Marketplace preview; only locally approved published product versions",
                )
            ],
            expires_at=utcnow() + timedelta(minutes=5),
        )

    async def scaffold(self, request, ctx):
        if request.template_version != ADAPTER_VERSION:
            raise ValueError("unsupported Foundry template version")
        return ctx.artifacts.put(ctx.tenant_id, create_archive(files_for(request.spec)))

    async def validate(self, request, ctx):
        try:
            validate_source(
                read_archive(ctx.artifacts.read(request.source, ctx.tenant_id)), request.spec
            )
            return ValidationReport(passed=True)
        except (ValueError, KeyError, TypeError):
            return ValidationReport(
                passed=False,
                diagnostics=[
                    Diagnostic(
                        code="INVALID_PALANTIR_SOURCE",
                        message="Expected an immutable Marketplace deployment descriptor",
                    )
                ],
            )

    async def build(self, request, ctx):
        if (
            ctx.toolchain_digest != TOOLCHAIN_DIGEST
            or not (await self.validate(request, ctx)).passed
        ):
            raise ValueError("Foundry descriptor/toolchain validation failed")
        return BuildArtifact(
            tenant_id=ctx.tenant_id,
            application_id=request.application_id,
            archive=request.source,
            source_digest=request.source.digest,
            spec_digest=digest(request.spec),
            spec=request.spec,
            adapter_version=ADAPTER_VERSION,
            toolchain_digest=TOOLCHAIN_DIGEST,
        )

    def _artifact(self, artifact, ctx):
        files_for(artifact.spec)
        if (
            artifact.tenant_id != ctx.tenant_id
            or artifact.spec_digest != digest(artifact.spec)
            or artifact.source_digest != artifact.archive.digest
            or artifact.adapter_version != ADAPTER_VERSION
            or artifact.toolchain_digest != TOOLCHAIN_DIGEST
        ):
            raise ValueError("Foundry artifact identity/toolchain mismatch")

    @staticmethod
    def _check_installation(native, b, identity):
        if native.get("ontologyRid"):
            raise NeedsAttention(
                "Foundry installation has an Ontology write destination outside this profile"
            )
        if (
            native.get("rid") != identity
            or native.get("marketplaceRid") != b["marketplace"]
            or native.get("productId") != b["product"]
            or native.get("parentFolderRid") != b["parent_folder"]
            or native.get("namespaceRid") != b["namespace"]
        ):
            raise NeedsAttention("Foundry installation identity or bindings changed")

    async def _snapshot(self, spec, application_id, ctx, client):
        b = bindings(spec, ctx.target)
        schema = await client.schema(spec)
        state = {"schema": schema, "installation": None}
        if ctx.owned_resources:
            if len(ctx.owned_resources) != 1:
                raise ValueError("Foundry delivery owns exactly one installation")
            binding = ctx.owned_resources[0]
            prefix = "palantir:" + digest(ctx.target.host) + ":"
            if (
                binding.key != "installation"
                or binding.resource_type != "palantir.marketplace-installation"
                or binding.application_id != application_id
                or binding.target_id != ctx.target.id
                or not binding.native_id.startswith(prefix)
            ):
                raise PermissionError("Foundry resource ownership mismatch")
            identity = rid(binding.native_id.removeprefix(prefix), "block-set-installation")
            native = await client.request("GET", BASE + "/" + identity)
            self._check_installation(native, b, identity)
            state["installation"] = native
        return state

    async def inspect(self, request, ctx):
        state = await self._snapshot(
            request.spec, request.application_id, ctx, self._client(ctx, request.spec)
        )
        return EnvironmentSnapshot(resources=list(ctx.owned_resources), fingerprint=digest(state))

    @staticmethod
    def _steps():
        return [
            PlanStep(key=key, action=action, resource_key="installation")
            for key, action in (
                ("palantir-installation-create", "create"),
                ("palantir-job-submit", "update"),
                (TERMINAL, "update"),
            )
        ]

    async def plan(self, request, ctx):
        artifact = request.artifact
        self._artifact(artifact, ctx)
        state = await self._snapshot(
            artifact.spec, artifact.application_id, ctx, self._client(ctx, artifact.spec)
        )
        return seal_plan(
            DeploymentPlan(
                tenant_id=ctx.tenant_id,
                application_id=artifact.application_id,
                target_id=ctx.target.id,
                connection_revision=ctx.target.connection_revision,
                artifact_id=artifact.id,
                artifact_digest=artifact.archive.digest,
                spec_digest=artifact.spec_digest,
                bindings_digest=bindings_digest(ctx.target),
                adapter_version=ADAPTER_VERSION,
                toolchain_digest=TOOLCHAIN_DIGEST,
                snapshot=EnvironmentSnapshot(
                    resources=list(ctx.owned_resources), fingerprint=digest(state)
                ),
                steps=self._steps(),
                details={"schema_digest": state["schema"]},
                expires_at=utcnow() + timedelta(minutes=15),
                digest="0" * 64,
            )
        )

    async def apply(self, request, ctx):
        artifact, plan = request.artifact, request.plan
        self._artifact(artifact, ctx)
        validate_plan(plan, ctx.target)
        validate_changes(plan.steps)
        if (
            plan.artifact_id,
            plan.application_id,
            plan.artifact_digest,
            plan.spec_digest,
            plan.adapter_version,
            plan.toolchain_digest,
            ctx.toolchain_digest,
        ) != (
            artifact.id,
            artifact.application_id,
            artifact.archive.digest,
            artifact.spec_digest,
            ADAPTER_VERSION,
            TOOLCHAIN_DIGEST,
            TOOLCHAIN_DIGEST,
        ):
            raise ValueError("Foundry plan/artifact/toolchain mismatch")
        if any(ctx.journal.has_intent(s.key) for s in plan.steps):
            raise NeedsAttention("Foundry native intent cannot be replayed; reconcile first")
        spec, client = artifact.spec, self._client(ctx, artifact.spec)
        b = bindings(spec, ctx.target)
        state = await self._snapshot(spec, artifact.application_id, ctx, client)
        if (
            digest(state) != plan.snapshot.fingerprint
            or plan.steps != self._steps()
            or plan.details != {"schema_digest": state["schema"]}
        ):
            raise ValueError("Foundry environment changed since planning")
        validate_source(read_archive(ctx.artifacts.read(artifact.archive, ctx.tenant_id)), spec)

        def begin(key):
            ctx.check_deadline()
            if not ctx.journal.intent(key, {"plan": plan.digest}):
                raise NeedsAttention("Foundry submission already attempted")

        def complete(key, handles):
            ctx.journal.complete(
                key,
                NativeStepReceipt(
                    step_key=key, plan_digest=plan.digest, resources=[binding], handles=handles
                ).model_dump(mode="json"),
            )

        begin("palantir-installation-create")
        if state["installation"]:
            native = state["installation"]
        else:
            native = await client.request(
                "POST",
                BASE,
                {
                    "marketplaceRid": b["marketplace"],
                    "productId": b["product"],
                    "parentFolderRid": b["parent_folder"],
                    "installationSuffix": "shifde-"
                    + digest({"app": str(artifact.application_id), "target": str(ctx.target.id)})[
                        :16
                    ],
                    "targetVersionHint": {"type": "versionId", "versionId": str(spec.version_id)},
                },
            )
        identity = rid(native["rid"], "block-set-installation")
        binding = ResourceBinding(
            key="installation",
            native_id=native_id(ctx.target.host, identity),
            resource_type="palantir.marketplace-installation",
            application_id=artifact.application_id,
            target_id=ctx.target.id,
        )
        # Persist the accepted identity before readback: metadata may be unavailable after acceptance.
        complete("palantir-installation-create", {"installation": identity})
        self._check_installation(await client.request("GET", BASE + "/" + identity), b, identity)
        payload = job_payload(identity, spec)
        begin("palantir-job-submit")
        accepted = await client.request("POST", JOBS, payload)
        job = rid(accepted["rid"], "installation-job")
        handles = {"installation": identity, "job_id": job, "request_digest": digest(payload)}
        complete("palantir-job-submit", handles)
        receipt = ApplyReceipt(
            tenant_id=ctx.tenant_id,
            application_id=artifact.application_id,
            target_id=ctx.target.id,
            artifact_digest=artifact.archive.digest,
            resources=[binding],
            operation_handles={
                **handles,
                "schema_digest": state["schema"],
                "spec_digest": digest(spec),
            },
        )
        validate_palantir_receipt(
            receipt, spec, ctx.target, completed=ctx.journal.completed, plan=plan
        )
        begin(TERMINAL)
        ctx.journal.complete(TERMINAL, receipt.model_dump(mode="json"))
        return receipt

    async def observe(self, request, ctx):
        receipt, spec = request.receipt, request.spec
        validate_palantir_receipt(receipt, spec, ctx.target)
        if receipt.tenant_id != ctx.tenant_id or receipt.target_id != ctx.target.id:
            raise PermissionError("Foundry receipt context mismatch")
        client = self._client(ctx, spec)
        h, b = receipt.operation_handles, bindings(spec, ctx.target)
        self._check_installation(
            await client.request("GET", BASE + "/" + h["installation"]), b, h["installation"]
        )
        job = await client.request("GET", JOBS + "/" + h["job_id"])
        if job.get("rid") != h["job_id"]:
            raise NeedsAttention("Foundry job identity changed")
        status = job.get("status")
        state = (
            "succeeded"
            if status == "SUCCEEDED"
            else "running"
            if status in {"PREPARING", "IN_PROGRESS", "BUILDING", "ABORTING"}
            else "unknown"
        )
        if state == "succeeded":
            installation = await client.request("GET", BASE + "/" + h["installation"])
            if (
                installation.get("latestTargetVersion", {}).get("versionId") != str(spec.version_id)
                or await client.schema(spec) != h["schema_digest"]
            ):
                state = "unknown"
        return NativeObservation(state=state, resources=receipt.resources)

    async def verify(self, request, ctx):
        observation = await self.observe(request, ctx)
        healthy = observation.state == "succeeded" and await self._client(
            ctx, request.spec
        ).verify_reads(request.spec)
        return VerificationReport(
            checks=[
                Check(
                    name="palantir-installation-job",
                    status="passed" if observation.state == "succeeded" else "failed",
                    evidence="pinned installation job status and identity/schema readback",
                ),
                Check(
                    name="palantir-frontend-and-ontology-read",
                    status="passed" if healthy else "failed",
                    evidence="approved static asset SHA-256 and read-only object access; no browser rendering or product publication asserted",
                ),
            ]
        )

    async def reconcile(self, request, ctx):
        return ReconciliationResult(
            state="ambiguous",
            resources=list(ctx.owned_resources),
            evidence="Inspect checkpointed installation/job RIDs and native audit logs; uncertain non-idempotent writes are never replayed",
        )

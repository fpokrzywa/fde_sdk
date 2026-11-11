"""Owned Fabric item lifecycle with durable submission/LRO checkpoints."""

import json
from datetime import timedelta
from uuid import UUID

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
from shifde_contracts.platform_specs.fabric import FabricApplicationSpec, FabricIQApplicationSpec
from shifde_core.artifact_store import create_archive, read_archive
from shifde_core.fabric_receipts import validate_fabric_receipt
from shifde_core.hashing import digest
from shifde_core.journal import NeedsAttention
from shifde_core.native import fabric_step
from shifde_core.planning import bindings_digest, seal_plan, validate_plan
from shifde_core.policy import validate_changes

from shifde_fabric.source import (
    ADAPTER_VERSION,
    COLLECTIONS,
    TOOLCHAIN_DIGEST,
    bindings,
    canonical_definition,
    definition,
    ordered,
    root,
    scaffold_files,
    validate_source,
)

TERMINAL = "fabric-finalize"


class FabricAdapter:
    kind = "fabric.solution/v1"

    def __init__(self, *, client_factory=None):
        self.client_factory = client_factory

    def _spec(self, spec):
        if (
            not isinstance(spec, (FabricApplicationSpec, FabricIQApplicationSpec))
            or spec.kind != self.kind
        ):
            raise ValueError("Fabric adapter profile/spec mismatch")
        return spec

    def _client(self, ctx):
        ctx.check_deadline()
        if not self.client_factory:
            raise ValueError("Fabric native credentials are unavailable")
        client = self.client_factory(ctx.target)
        client.iq = self.kind == "fabric.iq/v1"
        client.deadline = ctx.deadline
        client.auth()
        return client

    async def capabilities(self, ctx):
        try:
            client = self._client(ctx)
            workspace = str(UUID(ctx.target.bindings["workspace"]))
            info = await client.workspace(workspace)
            status = "supported" if info.get("capacityId") else "unknown"
        except Exception:
            status = "unknown"
        return CapabilityReport(
            adapter_version=ADAPTER_VERSION,
            application_kinds=[self.kind],
            capabilities=[
                Capability(
                    operation="metadata",
                    status=status,
                    reason="workspace/capacity metadata only; item permissions and tenant enablement remain native prerequisites",
                ),
                Capability(
                    operation="deployment",
                    status="unknown",
                    reason="experimental; native item writes require live acceptance",
                ),
            ],
            expires_at=utcnow() + timedelta(minutes=5),
        )

    async def scaffold(self, request, ctx):
        if request.template_version != ADAPTER_VERSION:
            raise ValueError("unsupported Fabric template version")
        return ctx.artifacts.put(
            ctx.tenant_id, create_archive(scaffold_files(self._spec(request.spec)))
        )

    async def validate(self, request, ctx):
        try:
            validate_source(
                read_archive(ctx.artifacts.read(request.source, ctx.tenant_id)),
                self._spec(request.spec),
            )
            return ValidationReport(passed=True)
        except (ValueError, KeyError, TypeError, UnicodeError):
            return ValidationReport(
                passed=False,
                diagnostics=[
                    Diagnostic(
                        code="INVALID_FABRIC_SOURCE",
                        message="Fabric source does not match the supported native definition profile",
                    )
                ],
            )

    async def build(self, request, ctx):
        if (
            ctx.toolchain_digest != TOOLCHAIN_DIGEST
            or not (await self.validate(request, ctx)).passed
        ):
            raise ValueError("Fabric source/toolchain validation failed")
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
        self._spec(artifact.spec)
        if (
            artifact.tenant_id != ctx.tenant_id
            or artifact.spec_digest != digest(artifact.spec)
            or artifact.source_digest != artifact.archive.digest
            or artifact.adapter_version != ADAPTER_VERSION
            or artifact.toolchain_digest != TOOLCHAIN_DIGEST
        ):
            raise ValueError("Fabric artifact identity/toolchain mismatch")

    @staticmethod
    def _identity(binding, workspace):
        prefix = f"fabric:{workspace}:"
        if not binding.native_id.startswith(prefix):
            raise PermissionError("Fabric resource belongs to another workspace")
        return str(UUID(binding.native_id.removeprefix(prefix)))

    @staticmethod
    def _binding(item, identity, workspace, application_id, ctx):
        return ResourceBinding(
            key=item.key,
            native_id=f"fabric:{workspace}:{UUID(identity)}",
            resource_type="fabric." + item.item_type,
            application_id=application_id,
            target_id=ctx.target.id,
        )

    @staticmethod
    def _check_native(native, item, workspace, name, identity=None):
        if (
            native.get("workspaceId") != workspace
            or native.get("type") != item.item_type
            or native.get("displayName") != name
            or identity
            and native.get("id") != identity
        ):
            raise NeedsAttention("Fabric item identity/type/name changed")
        return str(UUID(native["id"]))

    async def _snapshot(self, spec, application_id, ctx, client):
        workspace, names = bindings(spec, ctx.target)
        info = await client.workspace(workspace)
        if info.get("id") != workspace or not info.get("capacityId"):
            raise ValueError("Fabric workspace requires an assigned capacity")
        inventory = await client.items(workspace)
        owned = {r.key: r for r in ctx.owned_resources}
        if set(owned) - {item.key for item in spec.items}:
            raise ValueError("removing managed Fabric items requires a separate migration")
        state = {"workspace": workspace, "capacity": info["capacityId"], "items": {}}
        for item in ordered(spec):
            binding = owned.get(item.key)
            if binding:
                if (
                    binding.application_id != application_id
                    or binding.target_id != ctx.target.id
                    or binding.resource_type != "fabric." + item.item_type
                ):
                    raise PermissionError("Fabric owned resource context mismatch")
                identity = self._identity(binding, workspace)
                native = await client.item(workspace, item.item_type, identity)
                self._check_native(native, item, workspace, native["displayName"], identity)
                if any(
                    n["id"] != identity and n["displayName"] == names[item.key] for n in inventory
                ):
                    raise PermissionError("Fabric display name conflicts with an unowned item")
                state["items"][item.key] = {
                    "id": identity,
                    "name": native["displayName"],
                    "type": native["type"],
                    "description": native.get("description", ""),
                }
                if item.item_type != "Lakehouse":
                    state["items"][item.key]["definition"] = canonical_definition(
                        await client.definition(workspace, item.item_type, identity), item.item_type
                    )
            else:
                if any(n["displayName"] == names[item.key] for n in inventory):
                    raise PermissionError("pre-existing Fabric item is unowned")
                state["items"][item.key] = None
        return state

    async def inspect(self, request, ctx):
        spec = self._spec(request.spec)
        state = await self._snapshot(spec, request.application_id, ctx, self._client(ctx))
        return EnvironmentSnapshot(resources=list(ctx.owned_resources), fingerprint=digest(state))

    @staticmethod
    def _steps(spec, state):
        steps = []
        for item in ordered(spec):
            existing = bool(state["items"][item.key])
            for phase in (
                ["metadata", "definition"]
                if existing and item.item_type != "Lakehouse"
                else ["metadata"]
            ):
                steps.append(
                    PlanStep(
                        key=fabric_step(item.key, phase + "-submit"),
                        action="update" if existing else "create",
                        resource_key=item.key,
                    )
                )
                steps.append(
                    PlanStep(
                        key=fabric_step(item.key, phase + "-complete"),
                        action="update",
                        resource_key=item.key,
                    )
                )
        if spec.verification_notebook:
            steps.append(
                PlanStep(
                    key="fabric-verification-submit",
                    action="start",
                    resource_key=spec.verification_notebook,
                )
            )
        steps.append(PlanStep(key=TERMINAL, action="update", resource_key=spec.items[-1].key))
        return steps

    async def plan(self, request, ctx):
        artifact = request.artifact
        self._artifact(artifact, ctx)
        client = self._client(ctx)
        if artifact.spec.verification_notebook:
            client.auth(execution=True)
        state = await self._snapshot(artifact.spec, artifact.application_id, ctx, client)
        workspace, names = bindings(artifact.spec, ctx.target)
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
                steps=self._steps(artifact.spec, state),
                details={"workspace": workspace, "names": names, "kind": self.kind},
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
            raise ValueError("Fabric plan/artifact/toolchain mismatch")
        if any(ctx.journal.has_intent(s.key) for s in plan.steps):
            raise NeedsAttention(
                "Fabric native intent requires reconciliation; submission cannot be replayed"
            )
        spec, client = artifact.spec, self._client(ctx)
        if spec.verification_notebook:
            client.auth(execution=True)
        workspace, names = bindings(spec, ctx.target)
        if plan.details != {"workspace": workspace, "names": names, "kind": self.kind}:
            raise ValueError("Fabric planned bindings changed")
        state = await self._snapshot(spec, artifact.application_id, ctx, client)
        if digest(state) != plan.snapshot.fingerprint or plan.steps != self._steps(spec, state):
            raise ValueError("Fabric native state changed since planning")
        files = read_archive(ctx.artifacts.read(artifact.archive, ctx.tenant_id))
        validate_source(files, spec)
        resources = {r.key: r for r in ctx.owned_resources}
        ids = {key: self._identity(value, workspace) for key, value in resources.items()}
        handles = {
            "workspace": workspace,
            "kind": self.kind,
            "verification": spec.verification_notebook or "",
        }
        preview_handles = dict(handles)
        placeholder = "00000000-0000-4000-8000-000000000000"
        for item in spec.items:
            preview_handles[item.key + ":name"] = names[item.key]
            preview_handles[item.key + ":description"] = (
                json.loads(files[root(spec) + item.definition_path]).get("description", "")
                if item.item_type == "Lakehouse"
                else "Managed by SHIFDE"
            )
            if item.item_type != "Lakehouse":
                preview_handles[item.key + ":definition"] = "0" * 64
        if spec.verification_notebook:
            preview_handles.update(job_id=placeholder, verification_item=placeholder)
        preview = ApplyReceipt(
            tenant_id=ctx.tenant_id,
            application_id=artifact.application_id,
            target_id=ctx.target.id,
            artifact_digest=artifact.archive.digest,
            resources=[
                self._binding(item, placeholder, workspace, artifact.application_id, ctx)
                for item in spec.items
            ],
            operation_handles=preview_handles,
        )
        if len(json.dumps(preview.model_dump(mode="json"))) > 16000:
            raise ValueError("Fabric receipt exceeds hosted checkpoint budget; split the solution")

        def begin(key):
            ctx.check_deadline()
            if not ctx.journal.intent(key, {"plan": plan.digest}):
                raise NeedsAttention("Fabric native step was already submitted")

        def complete(key, native_handles=None):
            ctx.journal.complete(
                key,
                NativeStepReceipt(
                    step_key=key,
                    plan_digest=plan.digest,
                    resources=list(resources.values()),
                    handles=native_handles or {},
                ).model_dump(mode="json"),
            )

        for item in ordered(spec):
            wanted = definition(item, spec, files, workspace, names, ids, artifact.application_id)
            metadata = {
                "displayName": names[item.key],
                "description": json.loads(files[root(spec) + item.definition_path]).get(
                    "description", ""
                )
                if item.item_type == "Lakehouse"
                else "Managed by SHIFDE",
            }
            existing = item.key in ids
            for phase in ["metadata", "definition"] if existing and wanted else ["metadata"]:
                submit_key = fabric_step(item.key, phase + "-submit")
                complete_key = fabric_step(item.key, phase + "-complete")
                begin(submit_key)
                base = f"/v1/workspaces/{workspace}/{COLLECTIONS[item.item_type]}"
                if phase == "definition":
                    accepted = await client.request(
                        "POST",
                        f"{base}/{ids[item.key]}/updateDefinition",
                        {"definition": wanted},
                        params={"updateMetadata": "false"},
                    )
                elif existing:
                    accepted = await client.request("PATCH", f"{base}/{ids[item.key]}", metadata)
                else:
                    payload = dict(metadata)
                    if wanted:
                        payload["definition"] = wanted
                    else:
                        if json.loads(files[root(spec) + item.definition_path]).get(
                            "enableSchemas", True
                        ):
                            payload["creationPayload"] = {"enableSchemas": True}
                    accepted = await client.request("POST", base, payload)
                    if accepted["status"] != 202:
                        identity = self._check_native(
                            accepted["body"], item, workspace, names[item.key]
                        )
                        ids[item.key] = identity
                        resources[item.key] = self._binding(
                            item, identity, workspace, artifact.application_id, ctx
                        )
                complete(
                    submit_key,
                    {"operation_id": accepted["operation_id"]}
                    if accepted.get("operation_id")
                    else {},
                )
                begin(complete_key)
                result = await client.wait(accepted, result_required=not existing)
                if item.key not in ids:
                    identity = self._check_native(result, item, workspace, names[item.key])
                    ids[item.key] = identity
                    resources[item.key] = self._binding(
                        item, identity, workspace, artifact.application_id, ctx
                    )
                evidence = {"metadata_digest": digest(metadata)}
                if wanted:
                    evidence["definition_digest"] = canonical_definition(wanted, item.item_type)
                complete(complete_key, evidence)
                native = await client.item(workspace, item.item_type, ids[item.key])
                self._check_native(native, item, workspace, names[item.key], ids[item.key])
                if native.get("description", "") != metadata["description"]:
                    raise NeedsAttention("Fabric metadata readback differs from source")
            handles[item.key + ":name"] = names[item.key]
            handles[item.key + ":description"] = metadata["description"]
            if wanted:
                expected = canonical_definition(wanted, item.item_type)
                if (
                    canonical_definition(
                        await client.definition(workspace, item.item_type, ids[item.key]),
                        item.item_type,
                    )
                    != expected
                ):
                    raise NeedsAttention("Fabric definition readback differs from deployed source")
                handles[item.key + ":definition"] = expected
        if spec.verification_notebook:
            key = "fabric-verification-submit"
            begin(key)
            identity = ids[spec.verification_notebook]
            accepted = await client.request(
                "POST",
                f"/v1/workspaces/{workspace}/items/{identity}/jobs/RunNotebook/instances",
                {},
                execution=True,
            )
            if accepted["status"] != 202:
                raise NeedsAttention("Fabric notebook execution acceptance is unknown")
            job = client.job_id(accepted["location"], workspace, identity)
            handles.update(job_id=job, verification_item=identity)
            complete(key, {"job_id": job})
            await client.pause(accepted["retry_after"])
        receipt = ApplyReceipt(
            tenant_id=ctx.tenant_id,
            application_id=artifact.application_id,
            target_id=ctx.target.id,
            artifact_digest=artifact.archive.digest,
            resources=list(resources.values()),
            operation_handles=handles,
        )
        validate_fabric_receipt(receipt, spec, ctx.target, completed=ctx.journal.completed)
        begin(TERMINAL)
        ctx.journal.complete(TERMINAL, receipt.model_dump(mode="json"))
        return receipt

    async def observe(self, request, ctx):
        receipt = request.receipt
        validate_fabric_receipt(receipt, self._spec(request.spec), ctx.target)
        if (
            receipt.tenant_id != ctx.tenant_id
            or receipt.target_id != ctx.target.id
            or receipt.operation_handles.get("kind") != self.kind
        ):
            raise PermissionError("Fabric receipt context mismatch")
        client = self._client(ctx)
        workspace = str(UUID(receipt.operation_handles["workspace"]))
        if workspace != str(UUID(ctx.target.bindings.get("workspace", workspace))):
            raise ValueError("Fabric receipt workspace changed")
        for binding in receipt.resources:
            item_type = binding.resource_type.removeprefix("fabric.")
            identity = self._identity(binding, workspace)
            native = await client.item(workspace, item_type, identity)
            if (
                native.get("id") != identity
                or native.get("workspaceId") != workspace
                or native.get("type") != item_type
                or native.get("displayName") != receipt.operation_handles[binding.key + ":name"]
                or native.get("description", "")
                != receipt.operation_handles[binding.key + ":description"]
            ):
                return NativeObservation(
                    state="unknown",
                    resources=receipt.resources,
                    diagnostics=["Fabric native item drift requires reconciliation"],
                )
            expected = receipt.operation_handles.get(binding.key + ":definition")
            if (
                expected
                and canonical_definition(
                    await client.definition(workspace, item_type, identity), item_type
                )
                != expected
            ):
                return NativeObservation(
                    state="unknown",
                    resources=receipt.resources,
                    diagnostics=["Fabric native definition drift requires reconciliation"],
                )
        if receipt.operation_handles.get("verification"):
            identity, job = (
                str(UUID(receipt.operation_handles["verification_item"])),
                str(UUID(receipt.operation_handles["job_id"])),
            )
            state = (
                await client.request(
                    "GET",
                    f"/v1/workspaces/{workspace}/items/{identity}/jobs/instances/{job}",
                    read=True,
                )
            )["body"].get("status")
            if state != "Completed":
                return NativeObservation(
                    state="failed"
                    if state in {"Failed", "Cancelled", "Deduped"}
                    else "running"
                    if state in {"NotStarted", "InProgress"}
                    else "unknown",
                    resources=receipt.resources,
                )
        return NativeObservation(state="succeeded", resources=receipt.resources)

    async def verify(self, request, ctx):
        observation = await self.observe(request, ctx)
        return VerificationReport(
            checks=[
                Check(
                    name="fabric-native-definition-and-identity",
                    status="passed" if observation.state == "succeeded" else "failed",
                    evidence="definition/metadata readback and requested notebook job status; no implicit code execution",
                ),
                Check(
                    name="fabric-notebook-execution",
                    status="passed" if observation.state == "succeeded" else "failed",
                    required=True,
                    evidence="explicit notebook job completed",
                ),
            ]
            if request.receipt.operation_handles.get("verification")
            else [
                Check(
                    name="fabric-native-definition-and-identity",
                    status="passed" if observation.state == "succeeded" else "failed",
                    evidence="metadata/definition readback only; no notebook execution requested",
                )
            ]
        )

    async def reconcile(self, request, ctx):
        return ReconciliationResult(
            state="ambiguous",
            evidence="Inspect durable Fabric operation/job receipts and native state; never resubmit an uncertain mutation automatically",
            resources=list(ctx.owned_resources),
        )


class FabricIQAdapter(FabricAdapter):
    kind = "fabric.iq/v1"

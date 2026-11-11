"""Explicit Native App package/version/install lifecycle; no force or replacement path."""

import json
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
from shifde_contracts.platform_specs.snowflake import SnowflakeApplicationSpec
from shifde_core.artifact_store import create_archive, read_archive
from shifde_core.hashing import digest
from shifde_core.journal import NeedsAttention
from shifde_core.planning import bindings_digest, seal_plan, validate_plan
from shifde_core.policy import validate_changes

from shifde_snowflake.source import (
    ADAPTER_VERSION,
    TERMINAL_STEP,
    TOOLCHAIN_DIGEST,
    identifier,
    literal,
    names_for,
    native_id,
    prefix,
    quoted,
    scaffold_files,
    validate_source,
)


def relevant(state):
    fields = {
        "package": ("name", "created_on", "owner", "comment", "distribution"),
        "application": (
            "name",
            "created_on",
            "owner",
            "source",
            "source_type",
            "version",
            "patch",
            "label",
            "upgrade_state",
            "disablement_reasons",
        ),
        "stage": ("name", "created_on", "owner", "type", "schema_name"),
    }
    output = {"account": state["account"], "role": state["role"]}
    for key, allowed in fields.items():
        output[key] = {k: state[key].get(k) for k in allowed} if state.get(key) else None
    output["versions"] = sorted(
        (
            {
                k: v.get(k)
                for k in ("version", "patch", "label", "created_on", "dropped_on", "state")
            }
            for v in state.get("versions", [])
        ),
        key=lambda v: (str(v["version"]), int(v["patch"])),
    )
    return output


class SnowflakeAdapter:
    def __init__(self, *, client_factory=None):
        self.client_factory = client_factory

    def _client(self, ctx):
        ctx.check_deadline()
        if self.client_factory is None:
            raise ValueError("Snowflake native credentials are unavailable")
        return self.client_factory(ctx.target)

    @staticmethod
    def _spec(spec):
        if not isinstance(spec, SnowflakeApplicationSpec):
            raise ValueError("Snowflake adapter requires snowflake.native-app/v1")
        return spec

    async def capabilities(self, ctx):
        try:
            await self._client(ctx).execute(
                "SELECT CURRENT_ACCOUNT() AS ACCOUNT, CURRENT_ROLE() AS ROLE"
            )
            status, reason = (
                "supported",
                "native metadata connection verified; mutation privileges remain unproven",
            )
        except Exception:
            status, reason = "unknown", "native account metadata is unavailable"
        return CapabilityReport(
            adapter_version=ADAPTER_VERSION,
            application_kinds=["snowflake.native-app/v1"],
            capabilities=[
                Capability(operation="metadata", status=status, reason=reason),
                Capability(
                    operation="deployment",
                    status="unknown",
                    reason="requires package/install privileges and live acceptance",
                ),
            ],
            expires_at=utcnow() + timedelta(minutes=5),
        )

    async def scaffold(self, request, ctx):
        if request.template_version != ADAPTER_VERSION:
            raise ValueError("unsupported Snowflake template version")
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
        except ValueError:
            return ValidationReport(
                passed=False,
                diagnostics=[
                    Diagnostic(
                        code="INVALID_SOURCE", message="Snowflake managed source validation failed"
                    )
                ],
            )

    async def build(self, request, ctx):
        if ctx.toolchain_digest != TOOLCHAIN_DIGEST:
            raise ValueError("Snowflake build toolchain mismatch")
        if not (await self.validate(request, ctx)).passed:
            raise ValueError("Snowflake source validation failed")
        return BuildArtifact(
            tenant_id=ctx.tenant_id,
            application_id=request.application_id,
            archive=request.source,
            source_digest=request.source.digest,
            spec_digest=digest(request.spec),
            spec=self._spec(request.spec),
            adapter_version=ADAPTER_VERSION,
            toolchain_digest=ctx.toolchain_digest,
        )

    @staticmethod
    def _resource(key, resource_type, name, created, account, application_id, ctx):
        if not created:
            raise ValueError("native creation identity is missing")
        return ResourceBinding(
            key=key,
            resource_type="snowflake." + resource_type,
            native_id=native_id(resource_type, name, created, account),
            application_id=application_id,
            target_id=ctx.target.id,
        )

    def _resources(self, state, names, application_id, ctx):
        values = []
        for key, kind in (
            ("package", "application_package"),
            ("stage", "stage"),
            ("application", "application"),
        ):
            row = state.get(key)
            if row:
                name = names[key] if key != "stage" else names["package"] + ".SHIFDE.RELEASES"
                values.append(
                    self._resource(
                        key, kind, name, row["created_on"], state["account"], application_id, ctx
                    )
                )
        return values

    @staticmethod
    def _release(state, version, patch):
        return next(
            (
                v
                for v in state["versions"]
                if v["version"] == version and int(v["patch"]) == patch and not v.get("dropped_on")
            ),
            None,
        )

    def _check_ownership(self, state, names, app_id, ctx):
        for binding in self._resources(state, names, app_id, ctx):
            if binding not in ctx.owned_resources:
                raise PermissionError("refusing to adopt an unowned Snowflake object")
        known = {r.key for r in self._resources(state, names, app_id, ctx)}
        if any(
            r.key in {"package", "application", "stage"} and r.key not in known
            for r in ctx.owned_resources
        ):
            raise ValueError("owned Snowflake object is missing; resolve drift")
        if state.get("application") and (
            state["application"].get("source_type") != "APPLICATION PACKAGE"
            or state["application"].get("source") != names["package"]
        ):
            raise PermissionError("installed application belongs to another package")
        if state.get("stage") and state["stage"].get("type") != "INTERNAL":
            raise PermissionError("native source stage must be internal")

    def _check_artifact(self, artifact, ctx):
        self._spec(artifact.spec)
        if (
            artifact.tenant_id != ctx.tenant_id
            or artifact.archive.tenant_id != ctx.tenant_id
            or artifact.spec_digest != digest(artifact.spec)
            or artifact.toolchain_digest != TOOLCHAIN_DIGEST
            or artifact.adapter_version != ADAPTER_VERSION
        ):
            raise ValueError("Snowflake artifact provenance mismatch")

    async def inspect(self, request, ctx):
        spec = self._spec(request.spec)
        names = names_for(spec, ctx.target)
        if identifier(request.app_name) != names["application"]:
            raise ValueError("inspection binding mismatch")
        state = relevant(await self._client(ctx).snapshot(names))
        return EnvironmentSnapshot(
            resources=self._resources(state, names, request.application_id, ctx),
            fingerprint=digest(state),
        )

    async def plan(self, request, ctx):
        artifact = request.artifact
        self._check_artifact(artifact, ctx)
        spec, client = artifact.spec, self._client(ctx)
        names = names_for(spec, ctx.target)
        state = relevant(await client.snapshot(names))
        self._check_ownership(state, names, artifact.application_id, ctx)
        if state.get("application") and state["application"].get("upgrade_state") != "COMPLETE":
            raise ValueError("application upgrade is not complete")
        version, patch = identifier(spec.release_version), spec.release_patch
        release = self._release(state, version, patch)
        label = "SHIFDE_" + artifact.archive.digest
        if release and release.get("label") != label:
            raise ValueError("existing immutable version/patch has different content")
        release_key = f"release-{version}-{patch}"
        if release:
            owned_release = self._resource(
                release_key,
                "application_version",
                names["package"] + ":" + version + ":" + str(patch),
                release["created_on"],
                state["account"],
                artifact.application_id,
                ctx,
            )
            if owned_release not in ctx.owned_resources:
                raise PermissionError("refusing to adopt an unowned named release")
        app = state.get("application")
        if app and app.get("version") == version and patch < int(app.get("patch", 0)):
            raise ValueError("patch downgrade is not an upgrade")
        if patch > 0 and not any(
            v["version"] == version and not v.get("dropped_on") for v in state["versions"]
        ):
            raise ValueError("patch requires an existing named version")
        location = (
            "@"
            + ".".join((quoted(names["package"]), '"SHIFDE"', '"RELEASES"'))
            + "/"
            + artifact.archive.digest
        )
        release_key = f"release-{version}-{patch}"
        steps = [
            PlanStep(
                key="package-create",
                action="update" if state["package"] else "create",
                resource_key="package",
            ),
            PlanStep(key="schema-create", action="update", resource_key="package"),
            PlanStep(
                key="stage-create",
                action="update" if state["stage"] else "create",
                resource_key="stage",
            ),
            PlanStep(key="stage-upload", action="update", resource_key="stage"),
            PlanStep(key="setup-validate", action="update", resource_key="stage"),
            PlanStep(
                key="version-create",
                action="update" if release else "create",
                resource_key=release_key,
            ),
            PlanStep(
                key="application-apply",
                action="update" if state["application"] else "create",
                resource_key="application",
            ),
            PlanStep(key=TERMINAL_STEP, action="update", resource_key="application"),
        ]
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
                    resources=self._resources(state, names, artifact.application_id, ctx),
                    fingerprint=digest(state),
                ),
                steps=steps,
                details={
                    **names,
                    "version": version,
                    "patch": patch,
                    "label": label,
                    "stage_location": location,
                    "release_key": release_key,
                    "role": state["role"],
                    "existing_application": bool(state["application"]),
                    "existing_package": bool(state["package"]),
                    "existing_stage": bool(state["stage"]),
                    "existing_release": bool(release),
                },
                expires_at=utcnow() + timedelta(minutes=15),
                digest="0" * 64,
            )
        )

    async def apply(self, request, ctx):
        artifact, plan = request.artifact, request.plan
        self._check_artifact(artifact, ctx)
        validate_plan(plan, ctx.target)
        validate_changes(plan.steps)
        if (
            plan.artifact_id,
            plan.artifact_digest,
            plan.application_id,
            plan.spec_digest,
            plan.toolchain_digest,
            ctx.toolchain_digest,
        ) != (
            artifact.id,
            artifact.archive.digest,
            artifact.application_id,
            artifact.spec_digest,
            artifact.toolchain_digest,
            TOOLCHAIN_DIGEST,
        ):
            raise ValueError("plan/artifact/toolchain mismatch")
        for step in plan.steps:
            if ctx.journal.has_intent(step.key):
                raise NeedsAttention("Snowflake native checkpoint requires reconciliation")
        files = read_archive(ctx.artifacts.read(artifact.archive, ctx.tenant_id))
        validate_source(files, artifact.spec)
        files = {k.removeprefix(prefix(artifact.spec)): v for k, v in files.items()}
        client = self._client(ctx)
        names = names_for(artifact.spec, ctx.target)
        state = relevant(await client.snapshot(names))
        self._check_ownership(state, names, artifact.application_id, ctx)
        if digest(state) != plan.snapshot.fingerprint:
            raise ValueError("native Snowflake state changed since planning")
        d = plan.details
        if any(d[key] != value for key, value in names.items()) or d["role"] != state["role"]:
            raise ValueError("planned Snowflake bindings changed")
        resources = {
            r.key: r
            for r in ctx.owned_resources
            if r.key in {"package", "stage", "application", str(d["release_key"])}
        }

        async def checkpoint(key, action, refresh=False):
            if not ctx.journal.intent(key, {"plan": plan.digest}):
                raise NeedsAttention("native step was already submitted")
            ctx.check_deadline()
            result = await action()
            if refresh:
                current = relevant(await client.snapshot(names))
                expected_new = {
                    "package-create": "package",
                    "stage-create": "stage",
                    "application-apply": "application",
                }.get(key)
                for binding in self._resources(current, names, artifact.application_id, ctx):
                    if binding.key in resources and binding != resources[binding.key]:
                        raise NeedsAttention("native object identity changed during apply")
                    if binding.key not in resources and binding.key != expected_new:
                        raise NeedsAttention("unexpected native object appeared during apply")
                    if binding.key == "application" and (
                        current["application"].get("source") != names["package"]
                        or current["application"].get("source_type") != "APPLICATION PACKAGE"
                    ):
                        raise NeedsAttention("native application source object changed")
                    resources[binding.key] = binding
                release = self._release(current, str(d["version"]), int(d["patch"]))
                release_key = str(d["release_key"])
                if release:
                    binding = self._resource(
                        release_key,
                        "application_version",
                        names["package"] + ":" + str(d["version"]) + ":" + str(d["patch"]),
                        release["created_on"],
                        current["account"],
                        artifact.application_id,
                        ctx,
                    )
                    if release.get("label") != d["label"]:
                        raise NeedsAttention("native release content changed during apply")
                    if release_key in resources:
                        if binding != resources[release_key]:
                            raise NeedsAttention("native release identity changed during apply")
                    elif key != "version-create":
                        raise NeedsAttention("unexpected native release appeared during apply")
                    resources[release_key] = binding
                elif release_key in resources or key == "version-create":
                    raise NeedsAttention("native release disappeared during apply")
            receipt = NativeStepReceipt(
                step_key=key,
                plan_digest=plan.digest,
                resources=list(resources.values()),
                handles={"query_id": str((result or {}).get("query_id", ""))},
            )
            ctx.journal.complete(key, receipt.model_dump(mode="json"))
            return result

        async def unchanged():
            return {}

        marker = f"shifde:{ctx.tenant_id}:{artifact.application_id}:{ctx.target.id}"
        await checkpoint(
            "package-create",
            unchanged
            if d["existing_package"]
            else lambda: client.execute(
                "CREATE APPLICATION PACKAGE "
                + quoted(names["package"])
                + " DISTRIBUTION = INTERNAL ENABLE_RELEASE_CHANNELS = FALSE COMMENT = "
                + literal(marker)
            ),
            True,
        )
        await checkpoint(
            "schema-create",
            lambda: client.execute(
                "CREATE SCHEMA IF NOT EXISTS " + quoted(names["package"]) + '."SHIFDE"'
            ),
        )
        await checkpoint(
            "stage-create",
            unchanged
            if d["existing_stage"]
            else lambda: client.execute(
                "CREATE STAGE "
                + quoted(names["package"])
                + '."SHIFDE"."RELEASES" ENCRYPTION = (TYPE = \'SNOWFLAKE_SSE\')'
            ),
            True,
        )

        async def verify_stage():
            rows = (await client.execute("LIST " + str(d["stage_location"])))["rows"]
            if (
                len(rows) != len(files)
                or await client.download(list(files), str(d["stage_location"])) != files
            ):
                raise NeedsAttention("staged source bytes differ from the immutable artifact")

        async def upload():
            rows = (await client.execute("LIST " + str(d["stage_location"])))["rows"]
            if rows:
                await verify_stage()
            else:
                await client.upload(files, str(d["stage_location"]))
                await verify_stage()
            return {}

        await checkpoint("stage-upload", upload)

        async def validate_setup():
            result = await client.execute(
                "CALL SYSTEM$VALIDATE_NATIVE_APP_SETUP(" + literal(str(d["stage_location"])) + ")"
            )
            raw = next(iter(result["rows"][0].values()))
            report = json.loads(raw) if isinstance(raw, str) else raw
            if (
                not isinstance(report, dict)
                or report.get("status") not in {"SUCCESS", "PASS"}
                or report.get("errors")
            ):
                raise ValueError("native setup validation failed")
            return result

        await checkpoint("setup-validate", validate_setup)

        async def create_version():
            await verify_stage()
            if d["existing_release"]:
                return {}
            clause = (
                "ADD VERSION " + quoted(str(d["version"]))
                if int(d["patch"]) == 0
                else "ADD PATCH " + str(d["patch"]) + " FOR VERSION " + quoted(str(d["version"]))
            )
            result = await client.execute(
                "ALTER APPLICATION PACKAGE "
                + quoted(names["package"])
                + " "
                + clause
                + " USING "
                + literal(str(d["stage_location"]))
                + " LABEL = "
                + literal(str(d["label"]))
            )
            current = relevant(await client.snapshot(names))
            release = self._release(current, str(d["version"]), int(d["patch"]))
            if not release or release.get("label") != d["label"]:
                raise NeedsAttention("named release creation is unproven")
            return result

        await checkpoint("version-create", create_version, True)

        async def install():
            current = relevant(await client.snapshot(names))
            release = self._release(current, str(d["version"]), int(d["patch"]))
            if not release or release.get("label") != d["label"]:
                raise NeedsAttention("immutable release provenance changed")
            for binding in self._resources(current, names, artifact.application_id, ctx):
                if binding.key in resources and binding != resources[binding.key]:
                    raise NeedsAttention("native object identity changed before installation")
            if d["existing_application"] and not current["application"]:
                raise NeedsAttention("installed application disappeared before upgrade")
            release_binding = self._resource(
                str(d["release_key"]),
                "application_version",
                names["package"] + ":" + str(d["version"]) + ":" + str(d["patch"]),
                release["created_on"],
                current["account"],
                artifact.application_id,
                ctx,
            )
            if release_binding != resources.get(str(d["release_key"])):
                raise NeedsAttention("named version identity changed before installation")
            if d["existing_application"]:
                sql = (
                    "ALTER APPLICATION "
                    + quoted(names["application"])
                    + " UPGRADE USING VERSION "
                    + quoted(str(d["version"]))
                    + " PATCH "
                    + str(d["patch"])
                )
            else:
                sql = (
                    "CREATE APPLICATION "
                    + quoted(names["application"])
                    + " FROM APPLICATION PACKAGE "
                    + quoted(names["package"])
                    + " USING VERSION "
                    + quoted(str(d["version"]))
                    + " PATCH "
                    + str(d["patch"])
                    + " COMMENT = "
                    + literal(marker)
                )
            return await client.execute(sql)

        await checkpoint("application-apply", install, True)
        ctx.journal.intent(TERMINAL_STEP, {"plan": plan.digest})
        ctx.check_deadline()
        current = relevant(await client.snapshot(names))
        app_bindings = {
            r.key: r for r in self._resources(current, names, artifact.application_id, ctx)
        }
        if app_bindings.get("application") != resources.get("application"):
            raise NeedsAttention("installed application identity changed before role grant")
        await client.execute(
            "GRANT APPLICATION ROLE "
            + quoted(names["application"])
            + '."APP_USER" TO ROLE '
            + quoted(str(d["role"]))
        )
        receipt = ApplyReceipt(
            tenant_id=ctx.tenant_id,
            application_id=artifact.application_id,
            target_id=ctx.target.id,
            artifact_digest=artifact.archive.digest,
            resources=list(resources.values()),
            operation_handles={
                "package": names["package"],
                "application": names["application"],
                "warehouse": names["warehouse"],
                "version": str(d["version"]),
                "patch": str(d["patch"]),
                "label": str(d["label"]),
            },
        )
        ctx.journal.complete(TERMINAL_STEP, receipt.model_dump(mode="json"))
        return receipt

    async def observe(self, request, ctx):
        receipt = request.receipt
        if receipt.tenant_id != ctx.tenant_id or receipt.target_id != ctx.target.id:
            raise PermissionError("receipt target mismatch")
        names = {
            k: identifier(receipt.operation_handles[k])
            for k in ("package", "application", "warehouse")
        }
        state = relevant(await self._client(ctx).snapshot(names))
        app = state["application"]
        if not app or app.get("source") != names["package"]:
            return NativeObservation(state="failed")
        resource = self._resource(
            "application",
            "application",
            names["application"],
            app["created_on"],
            state["account"],
            receipt.application_id,
            ctx,
        )
        if resource not in receipt.resources:
            return NativeObservation(state="failed")
        if app.get("disablement_reasons") not in (None, "", "[]", "null", "NULL", []) or app.get(
            "upgrade_state"
        ) in {"FAILED", "DISABLED"}:
            return NativeObservation(state="failed", resources=receipt.resources)
        if app.get("upgrade_state") != "COMPLETE":
            return NativeObservation(state="running", resources=receipt.resources)
        matches = (
            app.get("version") == receipt.operation_handles["version"]
            and str(app.get("patch")) == receipt.operation_handles["patch"]
            and app.get("label") == receipt.operation_handles["label"]
        )
        return NativeObservation(
            state="succeeded" if matches else "unknown", resources=receipt.resources
        )

    async def verify(self, request, ctx):
        observed = await self.observe(request, ctx)
        passed = observed.state == "succeeded"
        if passed:
            h = request.receipt.operation_handles
            client = self._client(ctx)
            rows = (
                await client.execute(
                    "SELECT " + quoted(h["application"]) + '."APP_PUBLIC"."HEALTH"() AS STATUS',
                    warehouse=h["warehouse"],
                )
            )["rows"]
            passed = len(rows) == 1 and rows[0].get("status") == "OK"
            ui = (
                await client.execute(
                    "SHOW STREAMLITS IN SCHEMA " + quoted(h["application"]) + '."APP_PUBLIC"'
                )
            )["rows"]
            passed = passed and any(r.get("name") == "UI" for r in ui)
        return VerificationReport(
            checks=[
                Check(
                    name="installed-release-and-health",
                    status="passed" if passed else "failed",
                    required=True,
                )
            ]
        )

    async def reconcile(self, request, ctx):
        return ReconciliationResult(
            state="ambiguous",
            evidence="inspect native query history and durable checkpoints; no automatic mutation replay",
        )

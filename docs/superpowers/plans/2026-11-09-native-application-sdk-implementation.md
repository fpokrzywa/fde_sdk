# Native Application SDK Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Use superpowers:subagent-driven-development only if delegated execution is selected. Steps use checkbox syntax for tracking.

**Goal:** Deliver an external SDK and service that builds and deploys bounded, platform-specific native applications on Databricks, Palantir Foundry, Snowflake and Microsoft Fabric.

**Architecture:** Python API and adapters share a durable application service. Temporal coordinates immutable artifacts and target-specific deployment plans; isolated executors run native toolchains. TypeScript/Python SDKs and MCP expose the same authorized service operations.

**Tech Stack:** Python 3.12, FastAPI, Pydantic 2, Temporal Python SDK, PostgreSQL 17, SQLAlchemy 2/Alembic, Azure Blob/Key Vault behind interfaces, Node.js 24 LTS, TypeScript/pnpm, Docker, pytest/Vitest, OpenTelemetry.

**Spec:** [Native Application SDK Technical Architecture](../specs/2026-11-09-native-application-sdk-design.md). Read this spec before implementation. The paths and DTO names below refer to that document.

## Global Constraints

- The agent builds a different application for each platform. Cross-platform application portability is out of scope.
- Pydantic models are the canonical wire contracts.
- Build jobs install dependencies and execute generated code without deployment credentials.
- Unsupported or unknown required capabilities stop deployment.
- The plan has a 15-minute start TTL and explicitly reports unknown remote effects.
- Never guess based only on display name.
- Do not blindly replay mutations on 5xx or timeouts.
- No global automatic rollback: code reversion, schema recovery, and external side-effect repair are distinct native operations.
- A platform is `experimental` until its create, update, health, and failure-recovery suite passes in the relevant environment.
- No product implementation or live deployment is performed by creating this plan. Live checks use explicitly designated development targets and permitted test resources.

## Review Focus

- Native acceptance followed by worker death before persistence: reconcile rather than create a duplicate. Tasks 5 and 7–10.
- Cross-tenant object references, worker leases and credentials: reject even when IDs are valid. Tasks 3, 4 and 6.
- Source, binding or toolchain changes after planning: invalidate the plan rather than execute different bytes or authority. Tasks 1 and 5.
- CLI success followed by native startup failure or partial resource creation: deployment must not report success. Tasks 5 and 7–10.
- Expired leases, cancellation and concurrent writers: stale executors cannot advance state; reconcile residual effects. Tasks 5, 6 and 12.

## Delivery sequence

Tasks 1–6 produce a working delivery system using a deterministic fake adapter. Task 2 probes all platforms early. Tasks 7–10 add independently releasable native application kinds. Task 11 adds production client ergonomics; task 12 establishes the hosted/private execution release boundary. Do not wait until the final week to request customer development environments.

The initial estimate is 12–18 engineering weeks with two experienced engineers and timely administrator support. Treat it as a planning allowance for the bounded catalog, not a commitment. Re-estimate after Task 2. Defer a platform whose prerequisites fail rather than substituting a different application kind silently.

## Task 1 Establish contracts and adapter conformance

**Files:** Create `pyproject.toml`, `package.json`, `pnpm-workspace.yaml`, Python/Node lockfiles; `packages/contracts/src/shifde_contracts/{common,projects,builds,capabilities,plans,operations,execution}.py`; `packages/contracts/src/shifde_contracts/platform_specs/{databricks,palantir,snowflake,fabric}.py`; `packages/core/src/shifde_core/{adapter,registry}.py`; `packages/adapter_testkit/src/shifde_adapter_testkit/{conformance,fakes}.py`; `tools/export_contracts.py`; `tests/contracts/test_contracts.py`.

**Interfaces:** Produces every DTO and `PlatformAdapter` signature defined in the spec, `export_contracts(output_dir: Path) -> None`, and a fake adapter with controllable acceptance, timeout, crash and partial-success outcomes. Include stable `Application`, immutable `Project` revisions, typed `Deployment` results and the discriminated result union. `AdapterRegistry.get(application_kind: str, adapter_version: str) -> PlatformAdapter` selects a pinned registered adapter, never code from a request.

- [ ] Write contract tests asserting each of the four native specs accepts its valid fixture and rejects another platform's fields, unknown required operations remain unsupported, and native IDs preserve their vendor type. Test operation transition validation against the spec's exact states.
- [ ] Run `uv run pytest tests/contracts/test_contracts.py -q`; confirm the test module fails because the contracts are absent.
- [ ] Implement strict Pydantic DTOs, the adapter protocol, fake adapter and registry. Add immutable artifact/plan identity fields, revision fields and connection/target schemas. Establish Python package discovery and a minimal Node workspace as needed by these contracts. Pin tool/runtime versions and export schemas to `schemas/`.
- [ ] Run the same tests and `uv run python tools/export_contracts.py`; expect passing tests and deterministic JSON output on a second export. Check generated contract fixtures with actual JSON Schema validation.
- [ ] Commit the contract baseline, schema outputs and dependency locks as `feat: define native delivery contracts`.

## Task 2 Prove vendor authentication and delivery prerequisites

**Files:** Create `tools/probes/{databricks,palantir,snowflake,fabric}.py`; `docs/platform-feasibility.md`; adapter `manifest.json` and `capabilities.py` files at the four package roots specified in the design; `tests/adapters/test_capabilities.py`; `tests/live/test_feasibility.py`.

**Interfaces:** Consumes `CapabilityReport`, `ReadContext` and the native kind schemas. Produces `probe_capabilities(ctx: ReadContext) -> CapabilityReport` for each adapter and a release manifest listing tested image/CLI/SDK versions. Probe results distinguish unsupported from untested/unknown and carry capture/expiry timestamps.

- [ ] Write tests for available features, missing platform feature, forbidden permissions, expired credentials and unreachable target. Assert unreachable is `unknown`, not `supported`; assert Fabric identity support is item-specific; assert unavailable SuperRepo blocks only its kind.
- [ ] Run `uv run pytest tests/adapters/test_capabilities.py -q`; confirm absent probes fail.
- [ ] Implement read-only capability probes. In designated development targets, separately exercise a disposable minimal create/update/status path through documented vendor tools, record cleanup and evidence, and pin required toolchains. Record Palantir CLI installation/provenance and enrollment availability. Audit every credentialed CLI command for application-controlled hooks; prove credential-free build separation or block the untrusted route. Do not install guessed vendor packages or depend on browser-only operations.
- [ ] Run capability unit tests and `uv run pytest tests/live/test_feasibility.py -m live -q` with explicitly configured development targets. Missing access is a recorded blocked acceptance condition, never a passing live test. Produce a supported/experimental/blocked decision for each kind.
- [ ] Commit probes and sanitized feasibility findings as `feat: establish platform capability gates`.

## Task 3 Implement tenant identity and durable repositories

**Files:** Create `apps/api/src/shifde_api/auth.py`; `packages/core/src/shifde_core/persistence/{models,repositories,outbox,locks}.py`; `packages/core/src/shifde_core/{credentials,artifact_store}.py`; `migrations/`; `infra/compose.yaml`; `tests/api/test_tenant_isolation.py`; `tests/contracts/test_repositories.py`.

**Interfaces:** Consumes Task 1 DTOs. Produces `PrincipalContext`, `CredentialBroker.resolve(grant: CredentialGrant) -> CredentialHandle`, `ArtifactStore.put/get`, scoped repositories, `OperationRepository.create_with_outbox(...) -> Operation`, and `LockRepository.acquire/renew/release` with monotonic fencing tokens. Credential handles are available only to trusted execution code and cannot serialize to public DTOs.

- [ ] Load the applicable PostgreSQL skill before writing migrations. Write integration tests for tenant A referencing tenant B's artifact/connection; assert rejection. Assert operation and outbox entry commit atomically, stale lock renewal fails, and database tenant state does not leak through pooled connections. Assert overlapping managed namespaces are rejected and native resource aliases share the same lock key.
- [ ] Run `uv run pytest tests/api/test_tenant_isolation.py tests/contracts/test_repositories.py -q` against a local PostgreSQL container; confirm missing implementation fails.
- [ ] Implement the spec's persistence entities, composite tenant foreign keys, transaction-local row isolation, append-only audit role, verified JWT principal mapping and role checks. Configure JWT issuer, audience and key rotation; tests use a fixture issuer. Implement local artifact and fake vault providers for development; add Azure providers behind the same interfaces.
- [ ] Run the tests through the actual application database role, not a superuser; assert all pass. Verify connection updates require a matching revision and invalidate dependent plans. Check fake/real broker logs never include secret values.
- [ ] Commit as `feat: add tenant scoped delivery persistence`.

## Task 4 Build immutable source and artifact execution

**Files:** Create `packages/core/src/shifde_core/execution.py`; `executors/src/shifde_executor/{supervisor,sandbox,credentials,runner}.py`; `apps/api/src/shifde_api/routes/{projects,builds}.py`; `infra/containers/{build,deploy}.Dockerfile`; `tests/execution/test_artifacts.py`; `tests/execution/test_build_isolation.py`.

**Interfaces:** Produces `ExecutionJob`, `Executor.run(job: ExecutionJob) -> ExecutionResult`, source upload/grant verification, source archive materialization, and `BuildArtifact` creation. Consumes `BuildContext`, `BuildRequest`, `SourceArchive` and artifact-store interfaces. Project revision is immutable input to a build; deployment IDs do not define application identity.

- [ ] Write tests rejecting traversal, escaping symlinks, duplicate archive entries, arbitrary Git hosts and digest mismatch. Run a generated build that tries to read a deployment secret and assert it cannot. Assert a rebuilt artifact has a different digest when bytes or pinned build inputs change. Assert application-controlled deployment hooks are rejected even when the artifact digest is valid.
- [ ] Run `uv run pytest tests/execution/test_artifacts.py tests/execution/test_build_isolation.py -q`; confirm the unsupported paths fail.
- [ ] Implement safe extraction, immutable content-addressed storage, registered Git commit materialization and a typed argv-based runner without shell interpolation. Render templates locally; run application dependency installation/build in a fresh isolated job. Store provenance and dependency locks. Implement optional narrow metadata materialization as a separate job with read-only credentials.
- [ ] Run the tests with the container-backed executor, assert credentials are absent from build environment/history/artifacts, and assert compiled output/source archives can be retrieved only by the owning tenant. Record the stronger sandbox prerequisite before accepting untrusted multi-tenant builds.
- [ ] Commit as `feat: produce immutable application artifacts`.

## Task 5 Implement durable planning and deployment workflows

**Files:** Create `packages/core/src/shifde_core/{service,policy}.py`; `apps/control_worker/src/shifde_control_worker/{main,workflows,activities,dispatcher}.py`; `tests/workflows/{test_plan_binding,test_recovery,test_cancellation}.py`.

**Interfaces:** Produces `ApplicationService.submit(kind: OperationKind, request: RequestModel, principal: PrincipalContext, idempotency_key: str) -> Operation`; `PlanWorkflow`, `BuildWorkflow`, `DeployWorkflow`; canonical plan hashing; policy evaluation; deterministic outbox dispatch. Define `OperationKind` as `inspection/scaffold/validation/build/plan/deployment` and `RequestModel` as their Pydantic discriminated request union in `packages/contracts/src/shifde_contracts/operations.py`. Each operation's result type is declared in contracts. Adapter calls consume Task 1 request/context types through isolated executor jobs.

- [ ] Write tests for changed artifact, source/spec, target binding, connection revision, toolchain image and expired plan; each rejects before native mutation. Assert an unchanged plan runs. Inject native acceptance followed by worker death before receipt and assert reconciliation occurs without a second create. Assert ambiguous reconciliation becomes `needs_attention`.
- [ ] Run `uv run pytest tests/workflows -q` with Temporal's test environment; confirm absent workflows fail.
- [ ] Implement workflow/activity boundaries, outbox start with operation ID, step intent/checkpoints, lease-backed lock ownership, and policy decision binding. Add seven-day idempotency key retention with 409 on body mismatch. Enforce 15-minute plan start TTL and 60-minute default execution deadline. Cancellation stops future steps and inspects residual vendor state; resume starts reconciliation, not a blind rerun.
- [ ] Run the suite including duplicate outbox dispatch, lock expiry, stale executor, approval changes, Retry-After, startup failure, terminal-state immutability and deployment verification failure. Add two application plans trying to mutate the same native resource; assert exclusive ownership/locks prevent concurrent writes. Confirm no vendor I/O or credentials appear in deterministic workflow code/history.
- [ ] Commit as `feat: orchestrate recoverable native deployments`.

## Task 6 Expose API and execution gateway

**Files:** Create `apps/api/src/shifde_api/main.py`; `apps/api/src/shifde_api/routes/{connections,targets,applications,sources,inspections,scaffolds,validations,plans,policy_decisions,deployments,operations,execution_jobs}.py`; update project/build routes from Task 4; `tests/api/{test_operations,test_gateway,test_authorization}.py`; update `schemas/openapi.json`.

**Interfaces:** Implements every public endpoint and internal executor endpoint in the design through `ApplicationService`. Long operations return 202 plus Location and an `Operation`; immediate registrations return 201. Gateway methods lease jobs, record durable step events/receipts, resolve authorized credential grants, heartbeat and accept final results. No route directly invokes a vendor mutation.

- [ ] Write HTTP tests for 202/Location, ordered cursor events, ETag conflicts, invalid specs, unowned IDs, idempotency replay and mismatched body. Test a worker from the wrong pool/tenant, expired lease, stale fencing token and unknown image; each fails before credential resolution. Assert the executor cannot write natively before durable intent acknowledgement or advance before receipt acknowledgement; reject conflicting checkpoint replay with 409.
- [ ] Run `uv run pytest tests/api -q`; confirm missing routes fail.
- [ ] Implement routes, schema-based request validation, signed scoped upload grants, tenant-aware exception mapping, worker authentication and job-bound credential grants. Reject unknown checkpoint steps and monotonic-sequence regressions. Bound event/result payload size and redact diagnostics before persistence. Export OpenAPI.
- [ ] Run API/workflow tests together. Simulate gateway disconnect after checkpoint acceptance; replay must return the persisted checkpoint. Regenerate OpenAPI twice and expect no diff. Verify an HTTP client can complete a fake-adapter create/update operation end to end.
- [ ] Commit as `feat: expose delivery API and executor gateway`.

## Task 7 Deliver Databricks App adapter

**Files:** Create `packages/adapters/databricks/src/shifde_databricks/{adapter,schemas,inspect,validate,build,plan,apply,observe,verify,clients}.py`; `templates/databricks-app/`; `agent_packs/databricks/`; `infra/containers/databricks.Dockerfile`; `tests/adapters/test_databricks.py`; `tests/live/test_databricks_app.py`.

**Interfaces:** Implements `PlatformAdapter` for `databricks.app/v1`; consumes native warehouse/resource bindings. Produces native app, deployment and bundle ownership identifiers; deployment phases distinguish bundle apply, app start and app health. Capabilities from Task 2 complete the adapter.

- [ ] Write fixture-backed tests for stable bundle root, resource-key versus app-name distinction, planned bundle removals, separate app start, and command-success/startup-failure. Inject lost native acceptance receipt and assert lookup/reconciliation before retry.
- [ ] Run `uv run pytest tests/adapters/test_databricks.py -q`; confirm absent adapter fails.
- [ ] Implement SDK-backed inspection/status and a pinned CLI bundle workflow. Generate a Python health endpoint and a bounded existing-warehouse read. Keep bundle state authoritative for resources it owns. Verify deployer permissions separately from app runtime data permissions.
- [ ] Run unit/conformance tests, then `uv run pytest tests/live/test_databricks_app.py -m live -q`. Acceptance creates a development app, changes source, updates the same app ID, verifies authenticated health/data access, kills a worker at a checkpoint, and deploys the artifact to a second authorized target without rebuilding.
- [ ] Commit as `feat: deploy native Databricks apps`; mark the kind supported only for the tested configuration and versions.

## Task 8 Deliver Snowflake Native App adapter

**Files:** Create `packages/adapters/snowflake/src/shifde_snowflake/{adapter,schemas,inspect,validate,build,plan,apply,observe,verify,clients}.py`; `templates/snowflake-native-app/`; `agent_packs/snowflake/`; `infra/containers/snowflake.Dockerfile`; `tests/adapters/test_snowflake.py`; `tests/live/test_snowflake_native_app.py`.

**Interfaces:** Implements `snowflake.native-app/v1`; separates package stage, named version, installed instance and active release metadata. Uses immutable artifact files and explicitly quoted validated native identifiers. Instance transitions are structured DDL rather than forced CLI replacement.

- [ ] Write tests showing package upload alone cannot succeed the deployment, failed setup validation leaves accurate partial state, incompatible upgrade cannot drop/recreate the application, malformed identifiers are rejected, and a lost install result triggers inspection.
- [ ] Run `uv run pytest tests/adapters/test_snowflake.py -q`; confirm absent adapter fails.
- [ ] Generate manifest, setup SQL and Streamlit assets. Implement local build, CLI package synchronization, server validation as a deployment step, named-version creation and explicit install/upgrade. Discover CLI defaults/hooks that could write or prune and include them in the plan or disallow them. Never set a global force flag to bypass replacement confirmation.
- [ ] Run conformance and `uv run pytest tests/live/test_snowflake_native_app.py -m live -q` in a disposable development account. Create, verify, upgrade the same instance, inspect failure residue and demonstrate a rejected destructive replacement. Consumer marketplace publication is not acceptance for this task.
- [ ] Commit as `feat: deliver Snowflake native application packages`.

## Task 9 Deliver Fabric workspace solution adapter

**Files:** Create `packages/adapters/fabric/src/shifde_fabric/{adapter,schemas,inspect,validate,build,plan,apply,observe,verify,clients}.py`; `templates/fabric-solution/`; `agent_packs/fabric/`; `infra/containers/fabric.Dockerfile`; `tests/adapters/test_fabric.py`; `tests/live/test_fabric_solution.py`.

**Interfaces:** Implements `fabric.solution/v1` for lakehouse/notebook dependencies and bindings. Uses Azure Identity and HTTPX with explicit API timeouts. Produces persistent local-key/native-ID maps and long-running operation receipts. Verification distinguishes metadata reads from explicitly permitted notebook execution.

- [ ] Write tests for 201 versus 202 creation, operation Location and Retry-After, lakehouse-before-notebook ordering, rebinding to the target lakehouse ID, unsupported identity/item API, missing capacity, notebook-run authorization and lost creation receipt. Assert metadata success alone cannot pass a required execution check.
- [ ] Run `uv run pytest tests/adapters/test_fabric.py -q`; confirm absent adapter fails.
- [ ] Implement per-item capability checks, native definition readers/writers and durable LRO polling. Include bounded verification notebook execution as an explicit policy-bound final `apply` step using `DeployContext`, with its own journal/receipt/reconciliation; `verify` uses `ReadContext` to inspect the outcome only. Restrict the initial item catalog to tested lakehouse/notebook APIs and definition fields. Model credentials requiring a human step as unmet prerequisites.
- [ ] Run conformance and `uv run pytest tests/live/test_fabric_solution.py -m live -q`; create two related items, update the notebook without duplicating the lakehouse, execute the bounded check and demonstrate a second-target binding. Verify negative capacity/identity cases.
- [ ] Commit as `feat: deploy Fabric workspace solutions`; do not advertise custom Fabric UI support from this task.

## Task 10 Deliver feature gated Palantir adapter

**Files:** Create `packages/adapters/palantir/src/shifde_palantir/{adapter,schemas,inspect,validate,build,plan,apply,observe,verify,clients}.py`; `templates/palantir-superrepo/`; `agent_packs/palantir/`; `infra/containers/palantir.Dockerfile`; `tests/adapters/test_palantir.py`; `tests/live/test_palantir_superrepo.py`.

**Interfaces:** Implements `palantir.superrepo/v1` only where Task 2 proves SuperRepo and external CI support. Produces product/version/installation/frontend identifiers and a read-only OSDK-based verification profile. The enrollment-specific CLI version/provenance is part of artifact and executor compatibility.

- [ ] Write tests for unavailable SuperRepo, expired CLI token, a changed Ontology/OSDK materialization input, product-build success/install failure, frontend health failure and uncertain installation acceptance. Assert no fallback to a different app kind or implicit Ontology edit.
- [ ] Run `uv run pytest tests/adapters/test_palantir.py -q`; confirm absent adapter fails.
- [ ] Implement external build and deployment through enrollment-pinned documented CLI behavior, existing Ontology binding and OSDK generation. Materialize read-only SDK context before the credential-free build. Inspect installed product version and frontend/resource access after deployment. Retain experimental status if actual enrollment access is unavailable.
- [ ] Run conformance and `uv run pytest tests/live/test_palantir_superrepo.py -m live -q` in the authorized enrollment. Verify same-installation update semantics and read-only data access. Record beta feature/version constraints in capabilities and the release manifest.
- [ ] Commit as `feat: add feature gated Foundry application delivery`.

## Task 11 Publish SDK clients MCP tools and CLI

**Files:** Create `tools/generate_clients.py`; `packages/sdk_typescript/src/{generated,client,operations,index}.ts`; `packages/sdk_python/src/shifde_sdk/{client,operations}.py`; `apps/mcp/src/shifde_mcp/{server,tools,auth}.py`; `apps/cli/src/{index,commands}.ts`; `tests/api/test_mcp_parity.py`; `packages/sdk_typescript/tests/operations.test.ts`; `tests/contracts/test_sdk_python.py`; `examples/agent-native-app.ts`.

**Interfaces:** Produces `ShifdeClient`, generated endpoint groups, `operations.waitForResult` and typed API errors; all example methods in the design exist. MCP publishes the nine fixed lifecycle tools listed in the design plus `platform_schema`. CLI supports inspect, scaffold, build, plan, deploy, status and events through SDK calls; it does not contain vendor SDKs.

- [ ] Write tests for token refresh, deadline/AbortSignal, needs_attention, policy-wait state, terminal failure, result-kind validation and client cancellation that does not cancel server work. MCP and HTTP requests must yield equivalent policy/tenant outcomes. Assert MCP cannot use a privileged fallback identity.
- [ ] Run `pnpm --filter @shifde/sdk test` and `uv run pytest tests/contracts/test_sdk_python.py tests/api/test_mcp_parity.py -q`; confirm absent client/tool implementations fail.
- [ ] Generate transport/types from OpenAPI; implement handwritten wait helpers, credential providers and ergonomic facades. Generate clients with a pinned generator. Add MCP Streamable HTTP plus local stdio bridge and a thin CLI. Compile the example with actual SDK types and explicit runtime result validation.
- [ ] Run SDK tests, MCP parity tests, `pnpm --filter @shifde/sdk typecheck` and schema/client regeneration with a clean diff. Complete one fake-adapter deployment through each client and MCP; then run the available native examples against designated development targets.
- [ ] Commit as `feat: expose native delivery SDK and MCP interface`.

## Task 12 Release hosted and private executors

**Files:** Create `infra/azure/` modules for API, control worker, jobs, vault, artifact storage and PostgreSQL; `.github/workflows/{ci,release,live-adapters}.yml`; `tests/execution/{test_private_worker,test_failover}.py`; `docs/{operator-runbook,adapter-authoring,release-acceptance}.md`; `release-manifest.json`.

**Interfaces:** Hosted provisioner and customer supervisor implement the same gateway contract with pool/image constraints and fenced leases. Release manifest binds package/schema/template/adapter/CLI/worker-image versions and tested application kinds. Telemetry correlates operation, activity, executor and vendor request IDs without secrets.

- [ ] Write failure-injection tests for disconnect after accepted native mutation, lease expiry while the original process still runs, stale result submission, credential expiry, cancellation with residual native effects and artifact retention on active deployments. Assert a replacement worker does not mutate while the prior job is unaccounted for.
- [ ] Run `uv run pytest tests/execution/test_private_worker.py tests/execution/test_failover.py -q`; confirm missing lifecycle support fails.
- [ ] Implement outbound-only private polling, local vault resolution, hosted isolated job launch/termination and strong per-tenant isolation for untrusted builds. Add signed/pinned image policy, database backups and restore procedure, artifact garbage collection, operational alerts and redaction. Keep customer vault credentials and native data local where configured; document sanitized metadata crossing the gateway.
- [ ] Run all non-live checks, schema regeneration, sandbox failure tests and a database restore exercise. Run adapter live suites for every kind being released. A kind without live evidence remains experimental. Publish release artifacts only under the normal authorized release process; this plan does not authorize a production deployment or public package publication.
- [ ] Commit as `feat: release isolated hosted and private executors`.

## Verification commands and release evidence

From the repository root, the implemented project must support:

```text
uv sync --frozen
pnpm install --frozen-lockfile
docker compose -f infra/compose.yaml up -d
uv run pytest -m "not live" -q
pnpm --filter @shifde/sdk test
pnpm --filter @shifde/sdk typecheck
uv run python tools/export_contracts.py
uv run python tools/generate_clients.py
git diff --exit-code -- schemas packages/sdk_typescript/src/generated
```

CI checks Python lint/types with Ruff and mypy and TypeScript lint/types with the selected pinned tooling. Use isolated fixture schemas/namespaces for destructive tests and unique owned resources for live tests. HTTP recordings must redact tokens, data values and tenant-specific metadata. Live tests never pass merely because credentials are missing.

Acceptance evidence for each released native kind includes actual tool/image versions, create/update native IDs, second-target binding result, health checks, failure-recovery outcome and cleanup ownership. Source and plan review are not substitutes for this evidence.

## Explicit deferred work

Fabric custom workloads need a separate implementation plan covering hosted UI/backend, workload manifests, registration, tenant enablement and cloud resource lifecycle. Palantir Ontology authoring, new functions, Snowflake containers and consumer distribution each need new tested kinds/extensions. Do not hide these independent products inside a generic deploy function.

## Plan review and execution boundary

The design's builder review and a contract/coverage self-review precede delivery of these documents. Architecture choices are recommendations and can be changed before implementation. The present request is satisfied by this architecture and implementation plan; implementation, live credentials, production deployment and public publication are separate work.

# SDK foundation implementation progress

Plan: `docs/superpowers/plans/2026-11-09-native-application-sdk-implementation.md`

Scope authorized in this increment: project structure, shared contracts, core SDK foundation, then Databricks adapter. Other adapters, hosted infrastructure, PostgreSQL/Temporal service and MCP hosting remain subsequent increments.

Ruling: Work on `codex/sdk-foundation-databricks` in the current workspace. The repository is unborn (no HEAD), so a linked worktree cannot be created without first committing the existing design documents. Preserve those files and establish a feature-branch baseline instead of relocating the workspace.

Ruling: Execute the shared contracts portion of Task 1, local artifact/planning/journal primitives from Tasks 4–5, client foundations from Task 11, and the Databricks adapter from Task 7. Do not mark the full hosted-service tasks complete. This is the user's explicitly narrowed first increment.

Ruling: Windows-native commands and a tracked progress document replace the shell-based execution ledger scripts. Keep implementation and verification evidence here; no generated runtime state is committed.

Pre-flight: Contracts feed core, both clients and Databricks. Canonical wire models must be generated into TypeScript, not rewritten manually.

Pre-flight: The deployment plan requires a credential-free build. Initial build packages validated source without executing it; Databricks performs the final remote dependency installation. Local execution is intended for one trusted operator, not the planned hosted multi-tenant service.

Pre-flight: A bundle CLI success is not app health. App restart, native status and authenticated health/data checks remain separate steps.

Pre-flight: Databricks bundles can execute local hooks. Generate a bounded app-only deployment manifest and reject source-controlled bundle extensions rather than passing arbitrary source configuration to a credentialed CLI.

## Implemented increment

- Task 1 foundation: Python workspace packages, independent native schemas for all four platforms, adapter protocol/registry, canonical JSON Schema exports and generated TypeScript types. Native runtime and capability/operation contracts are implemented; the future service's complete HTTP resource catalog is not.
- Tasks 4–5 local slice: immutable checked ZIP archives, canonical plan binding, default change policy, durable step intent/receipt journal, local application ownership and unresolved-operation claims. No Temporal/PostgreSQL service or hosted tenant-isolation claim.
- Task 7 implementation: Databricks App scaffolding, source validation/packaging, read-only prerequisite probes, SDK inspection, pinned CLI bundle deployment and separate start, native receipt/status observation, conservative reconciliation and authenticated runtime warehouse health. Maturity remains experimental pending live acceptance.
- Task 11 client slice: Python and TypeScript HTTP clients, typed submissions/results, current token resolution, idempotency headers, deadline-bound polling, error handling and runtime validation. MCP hosting and a hosted delivery API remain deferred.
- Project structure, version manifest, operator documentation, an offline/local Databricks example, a compiled agent integration example and Windows/Linux CI are included.

## Verification and decisions

Ruling: Python packaging uses `--link-mode copy` in this OneDrive workspace because hardlinks can fail with Windows cloud-file error 396. pnpm's virtual store was placed under LOCALAPPDATA after a workspace linking stall. These are dependency-installation accommodations, not SDK runtime changes.

Ruling: The local source packager fingerprints the configured toolchain; it does not attest an OCI image or promise a reproducible vendor-side binary. Runtime requirements are pinned with hashes. The artifact holds deployment input source because Databricks installs dependencies remotely.

Evidence: Official Databricks CLI 1.25.0 was downloaded from its public release and its Windows archive SHA-256 checked. Generated bundle configuration passed the official release schema, using a Unicode-aware regex validator for Go-style schema patterns. The real SDK 0.163.0 statement serializer is exercised by the generated app health test. No live workspace credentials or native mutations were used.

Evidence: Offline scaffold/build commands completed; all four Python wheels built and imported in an isolated environment with packaged template/type markers present; the TypeScript package built and its agent example type-checked; schema/client regeneration produced identical bytes.

## Independent review and fix pass

The required fresh-context code review found no Critical issues and three Important recovery gaps. Each Important finding received a regression test observed failing before its fix:

- Current executor toolchain changes now reject the old plan before native mutation.
- Completed native receipts can repair local bookkeeping after the plan's start TTL, while retaining plan/target/artifact identity checks.
- A completed upload checkpoint without a completed start now requires attention because remote source provenance is unproven; recovery does not start potentially modified workspace bytes.

An additional regression prevents a completed journal being reused with a different sealed plan. Python polling now has literal overloads for build/plan/deployment results, addressing the review's typed-boundary comment as part of the requested SDK contract.

Final ruling: Hosted authorization, credential grants, leases and fencing remain deferred by the user's first-increment scope; local checks are not presented as their substitute.

Final ruling: Distributed cancellation and executor termination remain deferred; aborting SDK polling does not cancel a remote operation.

Final ruling: Live Databricks permission, create/update and recovery acceptance remain unvalidated because no designated live development target was provided; experimental status is retained.

Final ruling: Automatic resolution of ambiguous native writes remains excluded. The caller gets an attention-required outcome and durable records rather than an unsafe retry.

Final ruling: Palantir, Snowflake and Fabric have schema-only coverage in this increment; they are not registered as working deployment adapters.

Final ruling: Hosted hostile-code isolation and reproducible remote runtime installation remain outside the local source-packaging boundary.

## Final verification

- `uv run pytest -q`: 49 passed, including the four final plan/toolchain/recovery regression cases.
- `pnpm test`: 7 passed.
- Ruff lint and format checks: passed without cache use; mypy passed across packages, tools and examples.
- TypeScript SDK and agent example type checks: passed; TypeScript package build passed.
- All four Python workspace wheels: built successfully using copy mode. An isolated install imported the four packages and found the packaged native template and type markers.
- Generated schema/TypeScript output: byte-identical after regeneration. Official CLI 1.25.0 schema check and local offline scaffold/build smoke: passed.
- Live workspace deployment: not performed; experimental status retained. Hosted CI is defined but has not run remotely.

Final ruling: Preserve the `codex/sdk-foundation-databricks` branch. No merge, push, public publication or native application deployment is part of this increment.

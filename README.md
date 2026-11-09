# SHIFDE Native Application SDK

SHIFDE builds platform-specific applications through a shared delivery lifecycle. This first implementation provides canonical Python contracts, generated TypeScript contracts, HTTP SDK clients, a trusted local delivery service, and an experimental Databricks App adapter.

The Databricks adapter scaffolds a Python app, packages immutable source, plans changes against native state, deploys a bounded app-only bundle, starts the app, and verifies native status plus an authenticated runtime warehouse check. Local steps and ownership survive process restarts. An uncertain native write stops execution for reconciliation.

The hosted API, PostgreSQL/Temporal orchestration, isolated hosted/private executors and MCP server are subsequent increments. The HTTP SDKs target that API contract; this repository does not yet run that server. Palantir, Snowflake and Fabric have native specification models, not implemented deployment adapters. Local filesystem tenancy checks are not a hosted security boundary.

## Install and verify

Use Python 3.12, uv, Node.js 24 or 26, and pnpm 10.33.0. Run from the repository root:

```text
uv sync --frozen
pnpm install --frozen-lockfile
uv run pytest -q
uv run ruff check packages tools tests examples
uv run ruff format --check packages tools tests examples
uv run mypy packages tools examples --no-error-summary
uv build --all-packages --wheel --out-dir .tools/dist --link-mode copy
pnpm test
pnpm typecheck
pnpm --filter @shifde/sdk build
pnpm typecheck:examples
```

On Windows, a short dependency-store path can avoid OneDrive linking problems:

```powershell
pnpm install --frozen-lockfile --virtual-store-dir "$env:LOCALAPPDATA/shifde-sdk/node-store"
```

Use uv's `--link-mode copy` for package builds in OneDrive directories that reject hardlinks.

## Project layout

```text
packages/contracts/             Pydantic models and native specifications
packages/core/                  adapter protocol, artifacts, plans, policy and local journal/service
packages/adapters/databricks/   real Databricks SDK/CLI integration and packaged app template
packages/sdk_python/            validated async HTTP client
packages/sdk_typescript/        generated types, runtime validation and async HTTP client
schemas/                       canonical exported JSON Schemas
examples/                      local Databricks workflow and typed agent integration example
tests/                         contracts, SDKs, recovery and Databricks boundary tests
agent_packs/databricks/         development guidance for a calling agent
apps/ executors/ infra/         service boundaries reserved for subsequent increments
tools/                         contract generation and vendor-schema checks
```

The four native kinds are `databricks.app/v1`, `palantir.superrepo/v1`, `snowflake.native-app/v1` and `fabric.solution/v1`. Native schemas remain independent. The shared `PlatformAdapter` protocol exposes capabilities, inspection, scaffolding, validation, building, planning, applying, observing, reconciliation and verification.

## Run the Databricks workflow

Scaffolding and building are offline. They package source without executing it or resolving deployment credentials. Edit the generated `app/app.py` and dependencies; preserve the managed `databricks.yml` and `app/app.yaml` structure. Databricks performs the final dependency installation when the app is deployed.

```text
uv run python examples/databricks_app.py --target-file examples/target.example.json scaffold --output .shifde/app
uv run python examples/databricks_app.py --target-file examples/target.example.json build --source .shifde/app --application-id 33333333-3333-4333-8333-333333333333
```

For native access, create your own target file from `examples/target.example.json` and supply an actual development workspace origin, stable tenant/connection/target UUIDs and existing SQL warehouse ID. Keep the application and target IDs stable across updates. Configure Databricks SDK unified authentication for that workspace using a supported OAuth identity; the local driver resolves the current bearer token for each command. App resource binding requires the relevant native permissions; metadata visibility alone does not prove deploy privileges. The app's runtime principal has separate warehouse/data permissions.

Install the official **Databricks CLI 1.25.0** and put it on PATH, or supply `--cli` before the subcommand. The adapter checks the exact CLI version. A checksum-verified Windows copy downloaded during development is in ignored `.tools/databricks/`; binaries are not part of the SDK packages. [Official CLI installation](https://docs.databricks.com/aws/en/dev-tools/cli/install).

```text
uv run python examples/databricks_app.py --target-file .shifde/target.json plan
uv run python examples/databricks_app.py --target-file .shifde/target.json deploy
uv run python examples/databricks_app.py --target-file .shifde/target.json status
uv run python examples/databricks_app.py --target-file .shifde/target.json verify
```

Inspect `.shifde/plan.json` before running deploy. Plans expire after 15 minutes and bind the artifact, native spec, connection revision, environment bindings and toolchain. Deploying and starting are separate native actions. A returned receipt means native submission was observed; use status until it succeeds, then verify. Verification returns a failing exit status when health or runtime warehouse access fails.

The local driver persists source, artifacts, plans, receipts, ownership and intent/receipt journals under `.shifde/state/`. If native acceptance is uncertain, the same operation cannot be retried blindly and a fresh plan cannot bypass the unresolved claim. Inspect the native deployment and local records before recovery; automatic ambiguous recovery and rollback are deliberately absent. Keep local state when updating an existing managed app. No destructive cleanup is performed by the example.

A completed native receipt can finish local ownership bookkeeping after the plan's start TTL expires. An incomplete upload/start checkpoint instead requires attention: the adapter cannot prove that workspace source bytes remained unchanged after interruption, so it refuses to start them automatically. The current execution toolchain must match the artifact and sealed plan.

## SDK clients

Python imports `ShifdeClient` from `shifde_sdk`; TypeScript imports it from `@shifde/sdk` after building the package. Both expose `builds.create`, `plans.create`, `deployments.create`, operation lookup and deadline-bounded result polling. Submissions use canonical snake_case fields such as `project_id`, `artifact_id` and `target_id`. Supply an explicit idempotency key when resuming the same HTTP submission; clients do not automatically replay mutations.

The caller supplies its token provider and agent runtime. Tokens are resolved per request, redirects are refused, request/response shapes are validated, and error bodies are not echoed blindly. Aborting client polling does not cancel a server operation. Policy waits and ambiguous states surface as explicit attention-required errors. See `examples/agent-native-app.ts` for the typed agent boundary; it requires a separately deployed API service.

## Contracts and release status

Pydantic models are canonical. Regenerate tracked schemas and TypeScript output after changing them:

```text
uv run python tools/export_contracts.py
pnpm generate
```

The exported JSON Schemas enforce wire shape; Python validators also enforce semantic constraints such as Fabric dependency graphs and safe source paths. CI checks deterministic regeneration. No OpenAPI server document is emitted until the HTTP service exists.

Databricks is **experimental**: local tests exercise real adapter logic with controlled vendor boundaries, plus real SDK SQL serialization, CLI version and published bundle-schema checks. No live workspace deployment has been validated. To check a downloaded official CLI schema offline:

```text
uv run python tools/check_databricks_schema.py .tools/databricks/bundle-schema.json
```

The tested vendor versions and download checksum are in `release-manifest.json`. A live create/update/recovery test against a designated development workspace is required before changing adapter maturity to supported.

See the [architecture](docs/superpowers/specs/2026-11-09-native-application-sdk-design.md), [full implementation plan](docs/superpowers/plans/2026-11-09-native-application-sdk-implementation.md), and [increment progress](docs/implementation-progress.md).

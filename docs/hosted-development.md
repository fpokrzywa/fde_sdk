# Hosted delivery API, Temporal and MCP

The hosted path uses FastAPI, PostgreSQL 17 with transaction-local tenant RLS, Temporal's Python SDK and the official MCP Python SDK. The existing Databricks adapter performs native work. PostgreSQL owns operation state; Temporal owns durable coordination. A native receipt records submission. Success requires observation and runtime verification.

The API and MCP server call the same application service. Both verify RS256 bearer tokens against a configured issuer and audience, then check the caller's current database membership and scopes. JWT role claims are ignored. API and MCP audiences are separate. Native secrets are resolved only in execution workers.

## Start dependencies

Install the workspace with `uv sync --frozen --link-mode copy`. Install Databricks CLI **1.25.0** on execution hosts. Copy `.env.example` as a configuration reference and export its variables into each process environment. The application does not automatically load dotenv files.

Run PostgreSQL and Temporal locally, or use:

```text
docker compose -f infra/compose.dev.yml up -d
```

The Compose configuration is development only. It binds dependency ports to loopback and persists data in named volumes. The Temporal image runs the official [development server](https://github.com/temporalio/cli/blob/main/README.md). This checkout was verified against native PostgreSQL and Temporal processes; Docker is not installed in the development environment, so the Compose configuration has not been executed here.

## Provision the database

Use an operator connection for migration and membership provisioning. Keep this connection out of API/MCP/worker environments. With the development Compose database:

```text
SHIFDE_ADMIN_DATABASE_URL=postgresql+psycopg://postgres:shifde-development-only@127.0.0.1:5432/shifde
```

Export that value using your shell's syntax. Then run:

```text
uv run shifde-admin migrate
```

In an administrative SQL session, create a dedicated login with an actual generated password:

```sql
CREATE ROLE shifde_runtime LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD 'replace-me';
```

Grant the existing role access and create a tenant and initial membership:

```text
uv run shifde-admin grant-runtime shifde_runtime
uv run shifde-admin create-tenant --id 11111111-1111-4111-8111-111111111111
uv run shifde-admin set-member 11111111-1111-4111-8111-111111111111 your-oidc-subject owner
```

Set `SHIFDE_DATABASE_URL` to that runtime login. Startup rejects superuser and BYPASSRLS identities and an unsupported migration version. Tenant tables use FORCE RLS and composite tenant foreign keys. Events are append-only for the runtime role. Migration/provisioning remains an operator responsibility.

## Identity and process configuration

Configure `SHIFDE_AUTH_ISSUER` and exactly one of `SHIFDE_AUTH_JWKS_URL` or `SHIFDE_AUTH_JWKS_FILE` (a trusted JWKS JSON document). Tokens require `iss`, `aud`, `sub`, `iat`, `exp`, and `tenant_id`. The space-separated `scope` claim grants some combination of `shifde:read`, `shifde:write`, `shifde:deploy`, `shifde:admin`, `shifde:approve`; database roles further restrict those grants.

Readers can inspect and read; editors can build and plan; deployers can additionally deploy; owners can additionally register connections and approve. Deployments wait for approval from a different owner with `shifde:approve` when either the operator registry binding sets `require_approval=true` or the target environment requires it. Set that registry flag for every production connection: a caller-supplied environment label is not a security classification. The decision binds the plan digest and expiry. A decision cannot be replaced.

Use `SHIFDE_PUBLIC_URL` for the external HTTPS origin. Set API audience to the resource's configured API audience and MCP audience to `<public-origin>/mcp`. The MCP server publishes protected-resource metadata; your identity provider must support issuing resource-specific tokens. This repository does not implement an authorization server or interactive login broker.

For local dependency services only, set `SHIFDE_DEVELOPMENT=true` and `SHIFDE_TEMPORAL_TLS=false`. Public HTTP endpoints still require a trusted HTTPS proxy in front of port 8000. The proxy must preserve Host and Origin. Production settings require PostgreSQL `sslmode=verify-full` and Temporal TLS. Temporal settings support namespace, API key, optional root CA and mTLS certificate/key files.

API, control worker and execution worker must see the same artifacts. For a single host use the shared `SHIFDE_ARTIFACT_ROOT`. Across hosts install `uv sync --all-extras`, configure `SHIFDE_ARTIFACT_CONTAINER_URL` to an existing HTTPS Azure Blob container and grant the appropriate managed identities Blob access. The store checks tenant, digest, size and archive bounds on reads. Azure storage and Key Vault integration are implemented but have not been exercised against a live Azure account.

The credential registry maps opaque references to a tenant, exact workspace origin and credential source. Export `SHIFDE_CREDENTIAL_BINDINGS` as JSON using `.env.example`. Supported sources are environment PAT, environment OAuth M2M, or a PAT fetched from Key Vault with managed identity. Export actual credentials only into execution workers. API/MCP processes receive the registry's metadata, never its native secret values. A workspace origin can belong to only one hosted tenant in this implementation. Connections are immutable; target changes require `If-Match` and are blocked while native work is unresolved.

Run separate processes:

```text
uv run shifde-mcp
uv run shifde-worker control
uv run shifde-worker dispatcher
uv run shifde-worker execution
```

`shifde-mcp` serves both `/v1` and Streamable HTTP `/mcp` on port 8000. Alternatively `shifde-api` serves HTTP only. Do not bind both to the same port. Development mode permits `shifde-worker all`. Production uses separate control and execution queues/identities. API and MCP need PostgreSQL/artifact access; only coordination workers/dispatcher need Temporal connectivity. `shifde-worker execution` now dispatches isolated gateway jobs; enroll and run outbound supervisors using the [executor operating guide](executor-operations.md). Those supervisors need neither PostgreSQL nor Temporal access, and only they resolve native credentials.

For a local MCP client that supports only stdio, run `shifde-mcp --stdio` with an explicit `SHIFDE_API_URL` HTTPS origin and `SHIFDE_CALLER_TOKEN` for the API audience. It forwards that caller token to HTTP; there is no service-identity fallback. Refresh that environment token by restarting the bridge when needed.

## Delivery sequence

The machine-readable HTTP contract is `schemas/openapi.json`; native request models are in `schemas/contracts.json`.

1. Create an application, connection (owner only) and target. The connection's opaque credential reference must already exist in the operator registry. Targets derive their workspace origin from the connection.
2. Submit a scaffold, or create an upload grant, PUT ZIP bytes to `/v1/source-uploads/{id}/content`, then complete it using `/v1/source-upload-completions`. Grants expire in ten minutes and bind exact SHA-256 and size; ZIPs are capped at 20 MiB and validated before storage. Keep binary source transfer over HTTP; MCP tools pass references.
3. Create an immutable project using the source ID and platform-specific spec. Submit validation/build, inspect the operation, then plan against the target.
4. Submit deployment with artifact ID and plan ID. Every long-running submission requires `Idempotency-Key`; exact replays return the same operation, changed bodies conflict. Keys expire after seven days. HTTP returns `202` plus `Location`.
5. Poll operation state and cursor-based events. Approve when required. A returned native receipt is followed by native observation and verification before success.

MCP exposes the six operation submissions, operation/event lookup, capabilities/schema lookup, metadata creation, source grant/completion, cancellation, receipt recovery and policy decisions. Tool calls return operation references promptly; orchestration continues after the client disconnects.

## Failure and recovery semantics

Submissions commit resources, idempotency records, operation events and an outbox row in one PostgreSQL transaction. Dispatchers lease rows with `SKIP LOCKED`, use operation-based Temporal workflow IDs and fence acknowledgements by lease ID. Temporal outages leave committed work pending. Workflows carry IDs and coordination settings, not source, credentials or full plans.

Execution activities intentionally have one automatic attempt. Native writes persist intent before submission and receipt after acknowledgement. Current membership, deadline, cancellation and execution ownership are checked at write boundaries. A lost acknowledgement becomes `needs_attention`, retains its application/target claim, and blocks another deployment. Expiring a lease is not proof a remote side effect stopped.

Cancellation is cooperative. It prevents queued writes; after native acceptance, observation preserves the receipt and reports cancellation with native effects. It does not promise to delete or roll back a Databricks App. Resume is allowed only when a completed native run receipt proves submission; it delegates observation to the authorized recovering caller and refreshes the observation deadline without repeating native writes. An ambiguous native outcome requires operator investigation; arbitrary forced retry and destructive recovery are deliberately unavailable.

## Verify

Set `SHIFDE_TEST_ADMIN_URL` to an isolated PostgreSQL administrator connection allowed to create/drop test databases and roles, and `SHIFDE_TEST_TEMPORAL_ADDRESS` to a development Temporal server. Then run:

```text
uv run pytest -q
uv run ruff check packages apps tools tests examples
uv run ruff format --check packages apps tools tests examples
uv run mypy packages apps tools examples
pnpm test
pnpm typecheck
```

Without service configuration the hosted integration tests skip; CI has a separate job that provisions both services. Tests exercise actual PostgreSQL RLS under a non-superuser and actual Temporal workflows. Only Databricks SDK/CLI boundaries are faked. No live Databricks deployment or cloud infrastructure rollout is claimed.

The isolated executor supports the existing bounded source-only Databricks App adapter. It does not execute uploaded application code locally. Hosted and private outbound supervisors are implemented. General hostile-code compilation, broader platform adapters, production observability/quotas and cloud infrastructure provisioning remain later milestones.

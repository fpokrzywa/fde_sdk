# Hosted API Temporal and MCP implementation progress

Plan: `docs/superpowers/plans/2026-11-09-native-application-sdk-implementation.md`; baseline commit `0439204`.

Authorized increment: hosted API, durable Temporal orchestration using shared contracts and the Databricks adapter, and an MCP server exposing the same authorized operations.

Ruling: Continue in the existing checkout on `codex/hosted-api-temporal-mcp`. Keep the completed foundation branch intact. No remote push, cloud provisioning, publication or live Databricks mutations are requested.

Ruling: Keep framework/storage dependencies in a new `shifde-hosted` package; the shared contracts, adapter protocol and client SDK remain usable independently. API, control worker and MCP are separately packaged entry points over that service.

Ruling: PostgreSQL is the durable authority for resources, authorization memberships, idempotency, outbox, operations, events and native step receipts. Test actual RLS under a non-superuser role. SQLite is not a substitute for this verification.

Ruling: The existing source-only Databricks build never executes uploaded application code locally. Hosted native deployment runs in a separate worker queue with operator-configured tenant/host/credential bindings; API and MCP processes never resolve deployment credentials. General hostile-code compilation and outbound private executor provisioning remain the later executor milestone.

Pre-flight: Existing operations have only build/plan/deployment result variants; add inspection/scaffold/validation variants before exposing those operations through HTTP/MCP, then regenerate clients.

Pre-flight: Existing adapter contexts name concrete local stores/journals. Introduce structural interfaces so the hosted worker can use shared durable artifact storage and PostgreSQL journals without weakening the local implementation.

Pre-flight: Temporal retry is at least once. A failed or lost native write must not become permission to re-execute it. Persist execution ownership and journal intent, retain unresolved application claims, and distinguish completed-receipt metadata recovery from uncertain side effects.

Pre-flight: MCP tools must derive caller identity from verified bearer tokens and membership, use the same application service as HTTP, and never substitute worker credentials or a privileged service identity.

Implemented: canonical hosted DTOs and generated schemas; PostgreSQL/Alembic schema with FORCE RLS, tenant foreign keys, append-only event privileges, atomic submissions/outbox/idempotency; shared service and durable native journal; JWT/membership authorization; FastAPI API; deterministic Temporal workflow, separate control/execution activities and leased dispatcher; official MCP Streamable HTTP facade and caller-token stdio bridge; runtime Databricks credential factory; optional Azure Blob/Key Vault integration; operator provisioning CLI, entry points, development dependency Compose and hosted CI job.

Evidence before final review: 73 Python tests passed against actual PostgreSQL and Temporal, and all 7 TypeScript tests passed. Python type checks and TypeScript type checks passed. Native Databricks boundaries are faked; no live native or Azure/cloud deployment has occurred. Docker is unavailable, so Compose has only been statically checked.

Recovery regressions reproduced and fixed: refresh/redelegate observation from a completed native receipt when the original caller is revoked; reject replacement policy decisions. Cancellation after acceptance preserves native resources and reports native effects.

Independent read-only reviewer identified two Important defects: stale target read before locking, and caller-controlled environment labels bypassing production approval. Both were reproduced with failing real-PostgreSQL tests. Fixes reload/revalidate under target lock and bind mandatory approval to operator credential policy, rechecked by execution workers. No unresolved Critical/Important findings remain from that review.

Final verification: 76 Python tests passed without skips against the isolated real PostgreSQL/Temporal services; 7 TypeScript tests passed. Mypy passed for 64 source files; Ruff check and formatting passed for all packages/apps/tools/tests/examples. TypeScript SDK and example type checks/build passed. All eight Python wheels built; the hosted wheel includes migration Python and SQL resources. Operator/worker command entry points load in a clean environment outside OneDrive. Development Compose and CI YAML parse successfully; actual container execution and live Databricks/Azure tests are not claimed.

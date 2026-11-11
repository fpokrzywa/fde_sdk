# Microsoft Fabric and Fabric IQ adapters

The SDK has two experimental profiles sharing the canonical Fabric REST v1 transport:

| Kind | Managed resources |
| --- | --- |
| `fabric.solution/v1` | Lakehouse and Notebook items |
| `fabric.iq/v1` | Ontology items, optionally with Lakehouse/Notebook dependencies |

Fabric IQ here means ontology definition deployment. It does not implement every IQ workload, graph database, data agent, or custom Fabric UI. Native capacity, tenant settings and item permissions still determine whether an operation is available. No live Microsoft tenant has qualified these adapters as supported.

## Source and bindings

Use [the Fabric example](../examples/fabric-spec.example.json), or [the IQ example](../examples/fabric-iq-spec.example.json). Each item has a stable local `key`, native `item_type`, `definition_path`, and optional `depends_on`. Its `name_binding` defaults to its key. Notebook `lakehouse` must name a declared Lakehouse dependency. Dependencies are deployed in topological order. Removing managed items is rejected; deletion/migrations require a separate profile.

The connection host must be exactly `https://api.fabric.microsoft.com`. A target supplies `workspace` as a workspace UUID, plus each item's display-name binding:

```json
{"connection_id":"<connection-uuid>","bindings":{"workspace":"<fabric-workspace-uuid>","lake":"SalesLake","notebook":"SalesTransform","ontology":"SalesOntology"}}
```

The global Fabric host can serve multiple SDK tenants. Each workspace is reserved to one SDK tenant, atomically under PostgreSQL RLS, when creating or patching a target. Changing targets retains old reservations for recovery; there is no automatic ownership transfer. Existing Databricks/Snowflake account-host ownership remains unchanged. Only an operator should move a workspace between SDK tenants after checking native resources and unresolved operations.

The standard hosted sequence is application → scaffold → project → build → plan → deployment, through HTTP SDKs or the existing MCP server. Keep the same application and target IDs for updates. Build produces an immutable source archive and executes no customer code. Plans seal source/spec/toolchain, connection revision, workspace bindings and observed native state. Promotion uses the same artifact with a new target and a fresh plan.

Lakehouse source is JSON with `description` and `enableSchemas`. Schema enablement is a creation option; updates change metadata only and preserve data, native ID and schema mode. Shortcuts, table/schema changes and destructive replacement are outside this profile. Notebook source is nbformat-4 ipynb. The adapter injects the target workspace and managed Lakehouse ID into its default-lakehouse metadata. Hard-coded IDs in notebook code are not rewritten. Ontology source is a directory of generation-2 TMDL parts including `database.tmdl`, `model.tmdl` and `namespaces/default.tmdl`. The worker generates platform metadata; native Fabric performs semantic TMDL validation. Local validation checks packaging, UTF-8 and supported file paths, not the full TMDL grammar.

## Authentication and isolated execution

The initial profile restricts Lakehouse/Ontology display names to ASCII letters, numbers and underscores, beginning with a letter and under 100 characters. Notebook names are bounded to 128 characters. Builds pre-render definitions with placeholder UUIDs and check their transport budget; apply checks the complete receipt wire budget before its first native intent. Split solutions that exceed these limits.

Use canonical TMDL forms omitting service-elided defaults. This profile rejects explicit `type: explicit`, `type: Direct`, `type: singleColumn`, and `propertyScope: none` lines during build; omit these values rather than writing defaults Fabric discards on readback. Rule descriptions/synonyms that Fabric does not preserve are also rejected. This is a conservative lexical restriction, not a full TMDL parser. Native semantic validation and undocumented normalization still require live acceptance.

In ontology TMDL, use `{{fabric.workspace.id}}` and `{{fabric.item.<key>.id}}` for environment-specific UUIDs. Referenced item keys must appear in that ontology's `depends_on`. Substitution accepts only resolved UUIDs; unknown/unresolved references fail. Plain hard-coded TMDL IDs remain literal. Generated ontology logical IDs stay stable for the same application/local key across updates and promotion.

Register a pool with `fabric.solution/v1` and/or `fabric.iq/v1` in `application_kinds`, an allowed connection ID, and the current signed, digest-pinned executor image. Existing pools default to Databricks only. Hosted and private supervisors use the same outbound HTTPS gateway protocol. See [executor operations](executor-operations.md) for dedicated tenant VMs, egress controls, worker JWT refresh and receipt outbox storage.

Configure the opaque credential reference only on the execution host:

```json
{"fabric-private":{"tenant_id":"<sdk-tenant-uuid>","host":"https://api.fabric.microsoft.com","platform":"fabric","mode":"azure_client_secret","azure_tenant_id":"<entra-tenant-uuid>","azure_client_id":"<entra-application-uuid>","client_secret_env":"FABRIC_CLIENT_SECRET","allow_fabric_iq":true,"allow_notebook_execution":false}}
```

Supported Entra modes are `azure_client_secret`, `azure_managed_identity` (optional `azure_client_id` for a user-assigned identity), and `azure_workload_identity` (explicit tenant/client IDs and `azure_federated_token_file`). Azure Identity requests `https://api.fabric.microsoft.com/.default`. There is no interactive browser/device login or implicit developer-CLI credential fallback. `token_file`, `token_env` and local Key Vault token sources remain available for externally refreshed bearer tokens. Databricks `oauth_env` is rejected for Fabric.

The broker checks tenant, worker, pool, job, target, audience, platform and grant expiry, including after token acquisition. Fabric IQ requires `allow_fabric_iq=true`. Secrets and native bearer tokens stay on the worker host and inside its per-job RPC boundary; they are absent from API requests, artifacts, plans, operation events and Temporal payloads. Keep worker gateway identity separate from native Entra identity. Enforce HTTPS egress to the Fabric API and the required Entra/managed-identity token endpoints at the VM firewall.

The principal needs a Contributor-or-higher workspace role and appropriate item permissions. Enable the relevant tenant service-principal/Fabric item/ontology preview settings and assign supported Fabric capacity. App-only permissions and delegated scopes differ; do not infer actual write access from a successful metadata read. Microsoft documents service principal and managed identity support per endpoint; actual tenant feature rollout can still reject a particular operation.

## Long-running operations and recovery

Every native mutation has a durable intent. A 201 response records proven native identity directly. A 202 response records its operation UUID before polling. Polling reconstructs `/v1/operations/<uuid>` and `/result` on the canonical Fabric host; response Location/continuation URLs cannot redirect credentials. Retry-After and operation deadlines are bounded. Read throttling can retry; mutation submissions never retry automatically, including on 429 or uncertain network acceptance.

Intermediate receipts persist proven resource IDs through later failures. If a response is lost or polling is interrupted, the deployment becomes `needs_attention` and retains its application claim. The accepted operation ID remains in the checkpoint journal. Reconciliation is read-only and does not automatically restart an interrupted workflow. Account for native operations and pending local receipts before manual recovery; do not bypass uncertainty with a fresh application or deletion.

External administrators can still race updates: these APIs do not provide an atomic compare-and-swap covering an entire solution. Use one writer for managed items. Native item IDs are preserved on updates; unowned same-name objects, type changes, definition drift since planning and implicit adoption are rejected.

## Verification and tests

Accepted operation/job UUIDs are emitted in tenant-scoped operation events. Result-less updates use successful terminal state plus definition readback; create and getDefinition operations require native results. Created IDs are checkpointed before follow-up metadata GETs. Final receipt admission correlates the complete expected item set, metadata/definition digests and required job ID against the immutable spec and accepted checkpoints. Read-only Fabric verification requires that immutable spec explicitly and rejects incomplete evidence. Known local execution-policy denial is checked in plan, before the first apply intent and again at submission.

Default verification reads back item identity/name and deployed definitions. It proves persistence, not successful execution of arbitrary notebook code or usable ontology data. Cell metadata, declared runtime metadata and default Lakehouse bindings participate in notebook verification; execution outputs and platform metadata do not.

Set `verification_notebook` to a declared Notebook key to request an execution check. This adds an explicit plan-bound mutating job step and requires the local binding's `allow_notebook_execution=true`. Verification itself only reads the job status and cannot start code. Metadata success cannot satisfy a required execution check. The hosted operation deadline bounds monitoring; native Spark compute quotas/timeouts remain operator responsibilities. Cancellation/expiry can leave an already accepted native job running and requires reconciliation.

Run adapter/client/credential tests with `uv run pytest tests/adapters/test_fabric.py tests/adapters/test_fabric_clients.py tests/execution/test_fabric_credentials.py -q`. Hosted tests require PostgreSQL and Temporal. Fresh-container tests additionally require the current executor image in `SHIFDE_TEST_EXECUTOR_IMAGE`; they retain the real SDK/client/RPC and replace only the external Fabric HTTP boundary. These tests do not qualify live Azure authentication or native Fabric semantic validation.

For native acceptance, configure `SHIFDE_LIVE_FABRIC=1`, `SHIFDE_LIVE_API_URL`, a refreshed caller JWT at `SHIFDE_LIVE_API_TOKEN_FILE`, and distinct unused development targets in `SHIFDE_LIVE_FABRIC_TARGET_ID` and `SHIFDE_LIVE_FABRIC_IQ_TARGET_ID`. Their bindings must include `lake`/`notebook` and, for IQ, `ontology`. Run `uv run pytest tests/adapters/test_fabric_live.py -m live -q`. Explicitly enabled tests fail on missing configuration. They create and update native resources and execute the generated verification notebook. Cleanup is manual; never point acceptance at a production workspace.

Primary documentation: [Lakehouse creation](https://learn.microsoft.com/en-us/rest/api/fabric/lakehouse/items/create-lakehouse), [Notebook definitions](https://learn.microsoft.com/en-us/rest/api/fabric/articles/item-management/definitions/notebook-definition), [Ontology creation](https://learn.microsoft.com/en-us/rest/api/fabric/ontology/items/create-ontology), [TMDL ontology definitions](https://learn.microsoft.com/en-us/rest/api/fabric/articles/item-management/definitions/ontology-definition), [LRO state/result](https://learn.microsoft.com/en-us/rest/api/fabric/core/long-running-operations/get-operation-state), and [notebook job scheduling](https://learn.microsoft.com/en-us/rest/api/fabric/core/job-scheduler/run-on-demand-item-job).

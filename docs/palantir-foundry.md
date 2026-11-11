# Palantir Foundry delivery

Two profiles are deliberately separate. `palantir.marketplace/v1` is an experimental adapter for installing and updating an **already published, operator-approved product version** through the Foundry Marketplace REST v3 preview API. `palantir.superrepo/v1` stays gated: this enrollment has no approved SuperRepo CLI yet. Neither scaffolding nor building the Marketplace descriptor compiles or publishes an application.

The adapter supports a single existing local Marketplace product in a single destination namespace. It creates installation metadata, submits an installation job pinned to a UUID version, and updates the same SDK-owned installation on subsequent deployments. It never adopts an installation supplied in target bindings. It does not delete, abort, downgrade automatically, publish products, create Ontologies, apply Ontology overrides, supply credential inputs, or execute repository code. Product inputs use native defaults/automatic mappings; operators must review those effects before approving a release. Updates are native product upgrades, not frontend source edits.

## Descriptor and target

```json
{
  "kind": "palantir.marketplace/v1",
  "version_id": "11111111-1111-4111-8111-111111111111",
  "object_type": "Example",
  "frontend_asset_path": "assets/health.txt",
  "frontend_asset_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
}
```

Replace the sample UUID and digest with a published version and the SHA-256 of its immutable public frontend verification asset. Scaffold produces `deployment.json`; build validates and packages exactly that descriptor. Arbitrary source files and executable build hooks are rejected.

Connection `host` must be the exact enrollment HTTPS origin. Target bindings:

| Key | Value |
| --- | --- |
| `marketplace` | `ri.marketplace.main.local.<UUID>` |
| `product` | Published product UUID |
| `parent_folder` | `ri.compass.main.folder.<UUID>` |
| `namespace` | Expected `ri.compass.main.namespace.<UUID>` returned by installation metadata |
| `ontology` | Existing `ri.ontology.main.ontology.<UUID>`, used only for reads |
| `frontend_origin` | Exact operator-approved HTTPS origin |

Binding key names may be customized in the spec. Source and target never contain native credentials. All API, MCP and SDK operations reuse the shared scaffold/project/build/plan/deploy/inspect lifecycle. Use the platform schema key `palantir-marketplace`; `palantir` describes the gated SuperRepo profile. Workers must advertise `palantir.marketplace/v1` and explicitly permit the connection.

## Executor authentication and approval

The hosted control plane stores a `private` credential reference and execution pool ID. The supervisor resolves that reference locally. Hosted and private executors use the same contract: grant expiry, tenant, worker, pool, job, target and exact enrollment audience are checked before every resolution.

Local binding fields:

```json
{
  "tenant_id": "22222222-2222-4222-8222-222222222222",
  "host": "https://your-enrollment.example.com",
  "platform": "palantir",
  "mode": "palantir_client_credentials",
  "client_id_env": "FOUNDRY_OAUTH_CLIENT_ID",
  "client_secret_env": "FOUNDRY_OAUTH_CLIENT_SECRET",
  "palantir_scopes": [
    "api:v3:marketplace-installation:write:create",
    "api:v3:marketplace-installation:read:get",
    "api:v3:marketplace-installation-job:write:create",
    "api:v3:marketplace-installation-job:read:get",
    "api:ontologies-read"
  ],
  "allow_marketplace_preview": true,
  "palantir_approved_releases": []
}
```

Add one exact `palantir_approved_releases` entry per allowed release, containing all six resolved target bindings above plus `version_id`, `object_type`, `frontend_asset_path`, and `frontend_asset_sha256`. Empty approvals reject deployment. Each entry binds the product, destination, existing Ontology and frontend proof together. This configuration is operator-owned outside the API; an agent cannot approve its own arbitrary product or URL. Native product permission checks still apply. Approval authorizes the product's native behavior; it is not a product-content audit or a guarantee that default mappings are harmless.

OAuth client credentials are exchanged at the enrollment's `/multipass/api/oauth2/token`. The OAuth application's scopes and service user's native permissions must both permit the operations. `token_file`, `token_env`, or Key Vault references also support externally managed access tokens; temporary user tokens are suitable only for development. Databricks OAuth and Azure resource-token modes are rejected for this adapter. No native bearer is sent to the frontend origin. There are no automatic redirects or mutation retries.

## Health and recovery

Planning fingerprints read-only object-type schema and existing installation metadata. Apply rejects drift, validates the pinned descriptor, and writes durable intents before each native mutation. Accepted installation and job RIDs are checkpointed before follow-up reads. Terminal receipts bind those checkpoints to the artifact, source spec and requested native job payload.

Only `SUCCEEDED` installation jobs proceed to health checks. `PREPARING`, `IN_PROGRESS`, `BUILDING` and `ABORTING` remain running. `FAILED`, `ABORTED`, `IDLE_DRAFT` and unknown statuses retain the operation claim for reconciliation because native effects may be partial. A lost response never triggers automatic resubmission. Recovery exposes durable installation/job identifiers for operator investigation; it does not guess ownership or delete resources.

Health requires the pinned job's success, unchanged installation identity and schema, exact latest submitted version, a read-only object-list permission check, and the approved static frontend asset SHA-256. Native `latestTargetVersion` alone is not installed-version proof. HTTP 200 with an SSO login page fails the asset hash. This verifies asset availability and API access, not browser rendering or end-user interactive authorization. Foundry domain approval and frontend activation remain native prerequisites. A product published successfully can still have an unreachable frontend.

## Verification and prerequisites

Tests cover native HTTP request bodies, registration and updates, pinned versions, schema drift, uncertain write acceptance, no replay, job states, local release approval, credential audience and frontend proof. Hosted API tests use real PostgreSQL and orchestration; isolated-worker tests launch fresh Docker containers. Only the native HTTP service is simulated. No live enrollment acceptance has occurred; the profile remains experimental.

The opt-in live harness is `tests/adapters/test_palantir_live.py`. Enable only with `SHIFDE_LIVE_PALANTIR=1`, hosted `SHIFDE_LIVE_API_URL`, `SHIFDE_LIVE_API_TOKEN_FILE`, a configured `SHIFDE_LIVE_PALANTIR_TARGET_ID`, and `SHIFDE_LIVE_PALANTIR_SPECS_FILE` containing a JSON list of two distinct prepublished, locally approved Marketplace descriptors. This creates a real installation and upgrades it; it does not publish products or clean up native resources automatically.

To unlock source-to-product SuperRepo delivery later, supply enrollment enablement, a trusted pinned enrollment-distributed CLI, verified noninteractive commands/output contracts, OSDK/schema materialization, and a credential-free source build boundary. Do not install an unrelated public package named `foundry`.

Official references: [Foundry CLI](https://www.palantir.com/docs/foundry/superrepo/foundry-cli/), [prepare first deployment](https://www.palantir.com/docs/foundry/superrepo/prepare-first-deploy/), [create installation](https://www.palantir.com/docs/foundry/api/v3/marketplace-v3-resources/marketplace-installations/create-marketplace-installation/), [create installation job](https://www.palantir.com/docs/foundry/api/v3/marketplace-v3-resources/marketplace-installation-jobs/create-marketplace-installation-job/), [OAuth clients](https://www.palantir.com/docs/foundry/platform-security-third-party/writing-oauth2-clients/), [object reads](https://www.palantir.com/docs/foundry/api/ontologies-v2-resources/ontology-objects/list-objects/).

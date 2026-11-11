# Snowflake Native App adapter

The experimental `snowflake.native-app/v1` adapter creates an internal application package, uploads immutable source, adds a named version or explicit patch, installs an application, and upgrades that application in place. It uses the existing hosted API, Temporal workflows, execution gateway and isolated workers. The MCP server exposes the same submissions through the API; no additional native credential reaches the MCP process.

This profile supports provider-owned installation in the same Snowflake account. Marketplace listings, cross-account consumer rollout, release directives/channels, arbitrary setup SQL, migration authoring, destructive replacement and version pruning are outside this increment. New packages explicitly set `ENABLE_RELEASE_CHANNELS=FALSE`.

## Spec and target

Submit this spec when scaffolding and creating a project:

```json
{"kind":"snowflake.native-app/v1","project_root":".","package_binding":"package","application_binding":"application","warehouse_binding":"warehouse","release_version":"V1","release_patch":0}
```

Create a connection with the exact account origin, for example `https://organization-account.snowflakecomputing.com`, and an operator-registered opaque credential reference. Create a target for that connection:

```json
{"connection_id":"<connection-uuid>","bindings":{"package":"SHIFDE_DEMO_PACKAGE","application":"SHIFDE_DEMO_APP","warehouse":"DEPLOY_WH"}}
```

Names are single, unqualified identifiers, canonicalized to uppercase. Version names are limited to 30 characters; explicit patch numbers are limited to 0–129. Names, role, connection revision, source digest and toolchain are bound into the plan. Preserve application/target IDs across upgrades. Use a distinct target and distinct package/application names to promote the same immutable artifact into another environment.

Use the standard sequence: `POST /v1/applications`, `/v1/scaffolds`, `/v1/projects`, `/v1/builds`, `/v1/plans`, `/v1/deployments`. Poll each asynchronous operation at its returned Location. Change `release_patch` for a patch, or set a new `release_version` with patch zero. Existing version/patch content cannot be overwritten. Public SDK and MCP submissions use these same contracts.

## Source and native execution

The scaffolder generates `manifest.yml`, `setup.sql`, `ui/environment.yml`, and `ui/streamlit_app.py`. Edit the UI and supported UI assets; preserve the managed manifest, setup and environment exactly. Build packages source without importing customer Python or resolving dependencies. The managed setup creates an app-local versioned schema, health function, Streamlit object and application role. Customer dependency installation and UI execution occur in Snowflake.

The official connector is pinned to 5.2.0. Snowflake CLI 3.33.0 runs in its own virtual environment because its dependencies differ from the SDK runtime. Only primitive stage copies are allowed: no project hooks, `snow app deploy`, force replacement, source pruning or overwrites. Source is staged under its digest, downloaded and checked before creating a version. The final executor image contains both adapters and no API/database/Temporal runtime.

## Worker enrollment and credentials

Add `snowflake.native-app/v1` to the pool's `application_kinds`; existing pools default to Databricks only. Allow the connection ID and use the same signed digest-pinned executor image in the pool and supervisor. See [executor operations](executor-operations.md) for tenant isolation, egress firewall, worker identity and receipt outbox requirements.

Configure an operator-local binding in `SHIFDE_EXECUTOR_CREDENTIAL_BINDINGS`:

```json
{"snowflake-private":{"tenant_id":"<tenant-uuid>","host":"https://organization-account.snowflakecomputing.com","platform":"snowflake","mode":"token_file","token_file":"/run/shifde/snowflake-oauth","snowflake_account":"organization-account","snowflake_role":"SHIFDE_DEPLOYER","snowflake_user":"SHIFDE_SERVICE"}}
```

An external identity manager must atomically refresh the OAuth token file and restrict its permissions. The broker rereads it for each grant and checks tenant, worker, job, pool, target, host, expiry and platform. Environment-variable and local Key Vault token sources are also available; Databricks `oauth_env` is rejected for Snowflake. Account, role and optional user come from operator configuration. Tokens are passed to the connector in memory and to CLI through a short-lived mode-0600 file inside ephemeral job storage. Tokens never enter plans, artifacts, operation events or Temporal payloads. The worker still needs its separate gateway JWT.

Provision the deployment role with native privileges to create application packages/applications and operate its own package schema/stage, add versions, install/upgrade the managed app and grant its application role. Verification also needs USAGE on the bound warehouse. Effective OAuth scopes must permit that role. Grant only the designated package/application namespace; metadata visibility alone does not establish mutation privileges. Egress must allow the account endpoint and Snowflake stage storage endpoints needed for PUT/GET.

## Success and recovery

Package creation, source upload and version creation are intermediate checkpoints. Their resource identities are persisted even if subsequent validation or installation fails. Success requires the installed application's expected version/patch, completed upgrade state, matching source package and identity, a successful health-function query using the bound warehouse, and visibility of the expected Streamlit object. This checks native setup and metadata; it does not launch a browser or prove arbitrary customer UI behavior.

Pre-existing unowned native objects are rejected. Creation identities are checked between steps; a matching release label alone does not establish ownership. Unknown native acceptance retains the application claim and becomes `needs_attention`; the adapter does not retry native mutation blindly. Reconciliation is read-only. Keep the worker receipt outbox and control-plane database during recovery. Do not drop/recreate objects to bypass an unresolved claim.

Snowflake does not provide an atomic compare-and-swap across these DDL steps. An external administrator racing the managed workflow can still alter native objects between checks. Enforce a single writer for these names and investigate native query IDs when resolving uncertainty.

## Validation status

Local tests exercise package creation, setup rejection, explicit patches, new versions, in-place upgrades, partial receipts, uncertain install acceptance, ownership races and credential isolation. API/worker tests use real PostgreSQL, with a deterministic Snowflake boundary; container tests launch fresh hardened containers. Temporal tests exercise the durable workflow path. They do not establish live Snowflake compatibility.

Container tests use a bridge-local TCP relay for the host-side fake native service. Its persistent network namespace avoids Docker Desktop dropping reused client tuples when fresh containers receive the same bridge IP and source port. The relay forwards bytes without retrying requests; native state and credential checks remain in the existing test boundaries. Regressions force tuple reuse during patch schema creation and delay successful process exit while preserving the job deadline.

Before supported status, run the opt-in live acceptance test against a designated development account with the real connector/CLI and enrolled isolated worker. It creates resources and performs upgrades; cleanup is manual. No live account has been validated by this implementation.

Configure `SHIFDE_LIVE_API_URL`, `SHIFDE_LIVE_API_TOKEN_FILE` (refreshed API caller JWT), and `SHIFDE_LIVE_SNOWFLAKE_TARGET_ID`. The target must reference unused package/application names in the development account and an enrolled worker running the production image. Set `SHIFDE_LIVE_SNOWFLAKE=1`, then run `uv run pytest tests/adapters/test_snowflake_live.py -m live -q`. Explicitly enabled tests fail when configuration is incomplete; default local runs skip native writes. The test leaves the installed application and package versions for inspection. Manually inspect the Streamlit UI before qualifying support.

Native references: [package creation](https://docs.snowflake.com/en/sql-reference/sql/create-application-package), [versions and patches](https://docs.snowflake.com/en/sql-reference/sql/alter-application-package-version), [installation](https://docs.snowflake.com/en/sql-reference/sql/create-application), [upgrades](https://docs.snowflake.com/en/sql-reference/sql/alter-application), [stage copy](https://docs.snowflake.com/en/developer-guide/snowflake-cli/command-reference/stage-commands/copy), and [Native App Streamlit](https://docs.snowflake.com/en/developer-guide/native-apps/adding-streamlit).

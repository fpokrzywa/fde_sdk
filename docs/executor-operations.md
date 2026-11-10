# Isolated hosted and private executors

The production worker path now dispatches adapter calls through the execution gateway. The Temporal coordination activity has PostgreSQL access but resolves no native credentials and runs no native adapter. Each adapter action becomes an immutable job in an operator-registered pool. A supervisor polls the gateway over outbound HTTPS, launches a fresh container, renews its lease, mediates artifact/checkpoint requests and reports a typed result.

Hosted and private pools use the same protocol and supervisor. For hosted pools, run the supervisor on a dedicated tenant VM in your hosting environment. For private pools, the customer operates that VM inside its network. The customer VM needs outbound HTTPS to the gateway, its configured vendor hosts and its local identity/vault services; it needs no inbound port, PostgreSQL connection or Temporal connection.

## Security boundary

Production configuration requires an explicit dedicated-tenant Linux VM binding and a trusted Cosign public key. The supervisor verifies the digest-pinned image signature before every launch and fails closed on verification errors. Dedicated VM provisioning is an operator responsibility; a configuration flag is not hardware attestation. This increment does not provision Azure infrastructure or publish images.

Containers run as UID/GID 10001, with a read-only root, all Linux capabilities dropped, no new privileges, bounded CPU/memory/processes, ephemeral bounded `/tmp`, no host mounts and no Docker socket. Docker logging is disabled so the RPC protocol and bearer headers do not become daemon logs. The privileged supervisor/Docker daemon stay outside job containers. Build/scaffold/validation jobs use `--network none`; native jobs require a separately provisioned restricted network. The supervisor rejects Docker's unrestricted `host` and default `bridge` network names. **The operator must enforce destination allowlists using the VM/network firewall; a network name alone provides no egress policy.**

The Databricks build remains source-only packaging: it does not run uploaded application code or install customer dependencies locally. This is an isolation layer for the current adapter, not a general hostile-code compiler. Arbitrary multi-tenant build execution still requires a separately qualified VM/microVM build service. Development mode bypasses VM/signature admission checks explicitly and is unsuitable for hostile workloads.

## Build and pin the image

From the repository root, with Docker running:

```text
uv sync --frozen --link-mode copy
uv run python tools/build_executor_image.py --tag shifde-executor:dev
```

The helper builds only contracts/core/Databricks/executor wheels, exports hash-locked third-party dependencies and builds the Linux/amd64 image. The Dockerfile pins its Python base digest and checks the Databricks CLI 1.25.0 archive checksum. The build context excludes source checkouts, dotenv files, credentials and Git metadata. The final image contains no API/database/Temporal package.

Use the printed repository digest in the pool and supervisor settings. Some Docker storage backends assign no repository digest to a locally built image. For development/CI, use an operator-started loopback OCI registry and `--tag localhost:5000/shifde-executor:test --push-local`; the helper refuses non-loopback pushes. Production image signing and registry publication remain part of your approved release process. The supervisor uses `cosign verify --key <trusted-public-key-file> <image@sha256:...>`; provision the image locally before launching because execution uses `--pull never`.

The runtime flags follow the [Docker run reference](https://docs.docker.com/reference/cli/docker/container/run/); image verification follows [Cosign's verification interface](https://docs.sigstore.dev/cosign/verifying/verify/).

## Migrate and enroll

Upgrade the database as an operator, then re-grant the runtime role access to the new tables:

```text
uv run shifde-admin migrate
uv run shifde-admin grant-runtime shifde_runtime
```

Use the existing `SHIFDE_ADMIN_DATABASE_URL` only for these commands. API and coordination processes require migration `0002_executors`; workers never receive that administrator URL or the runtime URL.

Create a reviewed pool JSON using `examples/executor-pool.example.json`. Set its real tenant, image digest and allowed connection IDs. `mode` is `hosted` or `private`; it records placement without changing authorization. Register one eligible pool per action/connection. If a credential binding specifies `execution_pool_id`, route it only to that pool. Offline actions have no target connection; register one eligible offline pool for the tenant. Ambiguous routing fails closed.

```text
uv run shifde-admin register-pool examples/executor-pool.example.json
uv run shifde-admin register-executor 11111111-1111-4111-8111-111111111111 22222222-2222-4222-8222-222222222222 executor-service-subject --id 44444444-4444-4444-8444-444444444444
```

Registration is operator-only. Configure your identity provider to issue short-lived RS256 worker JWTs with:

- `aud`: `<public-api-origin>/executor` (separate from API and MCP audiences).
- `sub`: the exact registered worker subject; `tenant_id`, `worker_pool_id` and `executor_id`: the enrolled UUIDs.
- `scope`: `shifde:execute`; standard issuer, issued-at and expiry claims.

The gateway verifies signature/issuer/audience, then checks the current database worker/pool/tenant enablement. User tokens cannot lease jobs; worker tokens cannot call public API routes. Run an external workload-identity token manager that atomically refreshes the worker token file. The supervisor rereads it for each request; it does not implement an identity provider or interactive login flow.

For private native credentials, register the control-plane credential binding with `mode="private"`, tenant, exact host, `require_approval` and `execution_pool_id`. The hosted registry needs no customer vault URL, secret name or secret value. Use `require_approval=true` for production connections independently of caller-supplied environment labels.

## Configure the supervisor

Export these variables on the dedicated worker host, using your shell's syntax:

```text
SHIFDE_EXECUTOR_GATEWAY_URL=https://delivery.example
SHIFDE_EXECUTOR_TOKEN_FILE=/run/shifde/worker-token
SHIFDE_EXECUTOR_TENANT_ID=11111111-1111-4111-8111-111111111111
SHIFDE_EXECUTOR_POOL_ID=22222222-2222-4222-8222-222222222222
SHIFDE_EXECUTOR_WORKER_ID=44444444-4444-4444-8444-444444444444
SHIFDE_EXECUTOR_IMAGE=registry.example/shifde/executor@sha256:<real-digest>
SHIFDE_EXECUTOR_ISOLATION=dedicated_vm
SHIFDE_EXECUTOR_DEDICATED_TENANT_ID=11111111-1111-4111-8111-111111111111
SHIFDE_EXECUTOR_COSIGN_KEY=/etc/shifde/image-signing.pub
SHIFDE_EXECUTOR_NATIVE_NETWORK=tenant-vendor-egress
SHIFDE_EXECUTOR_STATE_DIR=/var/lib/shifde/receipts
```

Configure `SHIFDE_EXECUTOR_CREDENTIAL_BINDINGS` as JSON mapping the connection's opaque reference to a local binding:

```json
{"workspace-private":{"tenant_id":"11111111-1111-4111-8111-111111111111","host":"https://your-workspace.cloud.databricks.com","mode":"oauth_env","client_id_env":"PRIVATE_WORKSPACE_CLIENT_ID","client_secret_env":"PRIVATE_WORKSPACE_CLIENT_SECRET"}}
```

Supported local modes are `oauth_env`, `token_env` with a named environment variable, and `key_vault` with an HTTPS `vault_url` plus `secret_name`. Key Vault uses the supervisor's managed identity (`uv sync --all-extras` installs optional providers). PAT values are read locally without SDK discovery; OAuth headers use the official SDK's expiry/refresh behavior. SDK HTTP/retry budgets are bounded. Prefer short-lived OAuth or workload identity to long-lived PATs.

The gateway returns a short-lived job/worker/pool/tenant/target/audience-bound **reference grant**, not a native credential. The local broker checks that binding before resolving a secret. Only the trusted adapter receives the current bearer header, through an in-memory stdin pipe. It receives neither the worker JWT nor client secrets/vault identity. Credentialed adapter commands use regenerated reviewed configuration and reject source-supplied hooks. Source/build jobs cannot request native credential grants or upload arbitrary native result archives.

Protect the token file, environment provider and receipt directory with service-account ACLs. POSIX receipt files use mode 0600 and scoped directories; Windows deployments must also set service-account NTFS ACLs. No credentials are persisted in receipt files. The Databricks CLI necessarily receives a bearer token in its short-lived child environment inside the native job; application code is never executed in that credentialed path. Native grant expiry does not revoke an already issued PAT; bounded jobs and native principal privileges remain necessary.

Start:

```text
uv run shifde-executor
```

Run the API/MCP, dispatcher, Temporal control and coordination activities as described in `hosted-development.md`. `shifde-worker execution` now dispatches gateway jobs; the outbound supervisor performs native work. API/MCP/control processes require no native credentials.

## Failure, cancellation and recovery

Each poll carries a random request ID. Only retries of that exact request recover the existing lease; a restarted/new poll cannot relaunch an active job. Polls are serialized per worker identity. Leases renew every ten seconds by default and expire after 45 seconds; expiry never makes the same job eligible for a replacement executor. Stale lease/fence/identity/image requests cannot obtain grants or commit results.

Native intent must commit before an adapter write, and its receipt must commit before the next step. Checkpoints are sequence-checked, plan-bound and idempotent; conflicting replay is rejected. The supervisor atomically retains completed receipt metadata locally before submitting it and deletes it after acknowledgement. Disconnection stops new writes. A failed terminal result is withheld while a completion receipt is pending, so it cannot block replay during the valid lease. Pending receipts are replayed byte-for-byte on later iterations; an expired/fenced receipt stays on disk for investigation rather than bypassing lease checks.

An unaccounted-for apply job becomes `needs_attention` and retains its application claim. Even a seemingly failed container is not proof its vendor request failed. Use public operation events and native state to investigate. A completed durable bundle-run receipt enables the existing receipt-only resumption path; ambiguous acceptance cannot be forced into a blind redeploy. Take the worker offline and account for its containers before manual recovery. Preserve pending receipt metadata until the incident is resolved.

Cancellation is cooperative: heartbeat/guard failures stop new steps and terminate the job container. Native acceptance may already have occurred. Read-only observation after cancellation still records residual native resources; no delete/rollback is implied. VM-level service supervision must stop remaining containers if the supervisor is killed, because a process cannot execute its own `finally` block after host death. Leases prevent additional acknowledged work, and unresolved native claims remain retained.

## Verification and limits

Set the existing `SHIFDE_TEST_ADMIN_URL` and `SHIFDE_TEST_TEMPORAL_ADDRESS`, build the image, then export the real digest as `SHIFDE_TEST_EXECUTOR_IMAGE`:

```text
uv run pytest tests/execution -q
uv run pytest -q
```

Tests exercise real PostgreSQL tenant isolation, worker authentication, lease expiry, stale fences, checkpoint replay, local credential grants and real Temporal coordination. Docker tests verify confinement/timeout cleanup and complete source scaffolding/build through HTTP, the gateway, supervisor and real job containers. Native create/update/verification and uncertain acceptance use the real adapter with fake vendor boundaries; they do not establish live Databricks support. No cloud VM rollout, production image signature exercise or live Azure vault access is claimed.

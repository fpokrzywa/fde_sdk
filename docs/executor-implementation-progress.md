# Executor implementation ledger

Plan: docs/superpowers/plans/2026-11-09-native-application-sdk-implementation.md, executor portions of tasks 4, 6 and 12; specification: docs/superpowers/specs/2026-11-09-native-application-sdk-design.md. Baseline d1c1403; branch codex/isolated-private-executors.

Ruling: Extend the existing checkout on a new feature branch; preserve the hosted API baseline. No production provisioning, native deployment, or publication is authorized or necessary.

Ruling: Keep PostgreSQL and Temporal access in the trusted coordinator. Replace adapter calls with immutable gateway jobs, preserving the existing OperationExecutor state machine and journal. Private supervisors use outbound HTTPS only, with separate worker audience and tenant/pool/executor identity; no database or Temporal credentials leave the control plane.

Ruling: Leases expire fail-closed and are not reassigned automatically. An unaccounted-for native process retains its application claim and requires reconciliation. This sacrifices automatic failover availability to avoid duplicate native writes.

Ruling: The current Databricks adapter packages source without executing it. Fresh containers isolate these adapter jobs. Production requires signed digest-pinned images and dedicated tenant VM hosts; development containers must be explicitly enabled. General hostile-code compilation and broad cloud infrastructure release are separate work; containers alone are not advertised as a hostile multi-tenant security boundary.

Ruling: Native credentials stay in the supervisor's local vault/provider, resolved only after a job-bound gateway grant. A narrow stdin/stdout RPC channel supplies fresh bearer headers to trusted adapter code. Worker bearer identity, database credentials, host paths and Docker socket never enter job containers. Build/scaffold/validate jobs have no native credential or network access.

Pre-flight: Existing adapter methods are the isolation boundary; requests/results and contexts must be serializable. Artifact bytes cross scoped binary gateway routes; durable checkpoint acknowledgements cross authenticated JSON routes. Native writes require both lease validity and the existing operation execution fence.

Implementation sequence: contracts and database migration; worker authentication/leases/checkpoints/artifact grants; coordinator adapter proxy; isolated runner, local credential broker and supervisor; deployment images/configuration; end-to-end and failure-injection tests; independent final review and full verification.

Implemented: execution contracts; RLS-protected migration 0002_executors; registered worker/pool authentication under a separate JWT audience; immutable jobs, request-id-idempotent leases and fail-closed expiry; scoped artifact/grant/checkpoint/result routes; coordinator GatewayAdapter; outbound supervisor; fresh nonroot/read-only/network-bounded Docker runner; local PAT/OAuth/Key Vault broker; digest-pinned/hash-locked image; enrollment commands; receipt outbox; development and production operation documentation and CI container tests.

Evidence before final review: 15 execution tests passed, including actual PostgreSQL and Temporal, real container confinement/timeout cleanup, and HTTP/gateway/supervisor/container source builds. Native create/update and uncertain acceptance use the existing real Databricks adapter with fake vendor boundaries. Local PAT broker performs no SDK discovery; no native credential values enter gateway records.

Final review: independent fresh-context reviewer identified two Important receipt defects: premature terminal result blocking pending checkpoint replay (including stop-before-retention race), and missing directory fsync for receipt durability. Regression tests reproduced both. Fixed by retaining received completion metadata before stop checks, withholding terminal results while receipt metadata is pending, and syncing file/rename/directory creation/deletion before acknowledging durability. Production supervisors require Linux, a dedicated tenant VM binding, and a trusted signing key; development on Windows remains explicit.

Final: Ruling: actual dedicated-VM boundary, network firewall allowlists, signing-key custody, live vendor behavior and hostile-code compiler qualification were deliberately not judged by code review — retain explicit operator prerequisites and experimental native status rather than treating tests as deployment evidence — deploying without those prerequisites would invalidate the stated security boundary.

Final fixes: receipt regressions verified RED→GREEN, including completion arriving after stop, terminal-result withholding, restart replay and directory-sync failure. Actual Linux container probe confirms receipt file and directory fsync before acknowledgement and directory sync after deletion. Additional negative tests verify grant expiry during OAuth resolution and connection revocation during an active lease.

Final verification: 98 Python tests passed without skips against disposable real PostgreSQL/Temporal and the rebuilt Linux executor image; 7 TypeScript tests passed. Mypy passed for 79 source files; Ruff and formatting passed. TypeScript SDK/example checks and build passed; all nine Python wheels built. JSON Schema/OpenAPI/client regeneration is deterministic, CI YAML parses, and the development Compose configuration validates. Native SDK/CLI boundaries remain fake in create/update/failure tests; no live native or Azure deployment or production signing/VM qualification is claimed.

Final state: implementation retained on its feature branch with a local commit; no merge, remote push or publication. Disposable test services are stopped after verification; the built executor image and ignored wheel artifacts remain available locally.

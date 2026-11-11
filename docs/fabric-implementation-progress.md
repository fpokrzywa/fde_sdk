# Fabric and Fabric IQ implementation ledger

Scope: implement task 9 of the existing native SDK plan and the user's explicit Fabric IQ extension. Continue the shared, uncommitted Snowflake work without discarding it or claiming its unfinished validation is complete.

Ruling: use the current Python/HTTPX, Pydantic, Temporal and outbound isolated-worker foundation. Fabric IQ is a distinct `fabric.iq/v1` profile for Ontology items, sharing the Fabric REST transport and optionally including Lakehouse/Notebook dependencies. IQ preview features beyond Ontology are unsupported.

Ruling: target binding `workspace` is a Fabric workspace UUID; item display names are target bindings keyed by each item's `name_binding` (defaults to local key). Source is immutable and never executed locally. Lakehouse source controls display metadata and initial schema enablement only; updates never delete/recreate a Lakehouse or change its data/shortcuts/schema mode. Notebook definitions use ipynb; ontology definitions use generation-2 TMDL parts.

Ruling: no implicit adoption of native objects, no mutation retries, no redirects or arbitrary polling URLs. Every native submit has a preceding intent and a durable accepted receipt before polling. Poll only canonical Fabric operation UUID paths, honoring bounded Retry-After and the deadline. Lost acceptance requires attention; accepted operation IDs remain durable evidence. Runtime verification is an explicit plan-bound notebook job; default verification proves metadata/definition persistence only.

Implementation: contracts/source and failing tests; bounded REST/LRO client; plan/apply/observe/verify and IQ profile; Azure Identity credentials and platform dispatch; HTTP/Temporal/fresh-container tests; operator documentation and generation; independent final review and full regression checks. All adapters remain experimental until real tenant acceptance is configured.

Primary references: Microsoft Learn Fabric Lakehouse/Notebook/Ontology CRUD and definition APIs, core LRO state/result, job scheduler, Azure Identity. Official create APIs currently document service-principal and managed-identity support; tenant settings, workspace Contributor role and capacity remain native prerequisites.

Implementation evidence: absent-adapter scaffold tests RED→GREEN; 201/202 creation and dependency binding, in-place updates, lost native acceptance, durable LRO receipts, missing capacity, required notebook execution, IQ feature gate and throttling tests added. Notebook parameter-tag drift failed then passed after canonical definition checks included authored cell/runtime metadata. Direct IQ client factories now cannot omit the feature gate. Ontology environment token binding failed before implementation and passes with explicit declared dependency UUID substitution.

Ruling: Fabric's global API origin cannot use the existing account-host tenancy reservation. Reuse the PostgreSQL RLS-protected namespace table with canonical per-workspace reservations at target create/patch. Preserve other platform host reservations and old workspace reservations. Cross-tenant workspace ownership test RED→GREEN. Workspace binding is fixed to `workspace` so target admission can validate it before a project exists.

Focused API/Temporal/credential suite passed 28 tests, including two-target immutable artifact promotion for both profiles and absence of native bearer values in operation results/Temporal history. Initial production image built successfully. Fresh-container verification and whole-repository checks remain in progress; do not claim live native acceptance.

Final review: independent read-only review found nine Important issues; no Critical. One fix pass reproduced 18 failures (plus one already-passing drift case), then passed the review suite. Fixed: result-less update LROs; non-schema Lakehouse request shape; pre-rejection of service-elided TMDL defaults; runtime/environment/kernelspec/description drift; execution-policy preflight; complete terminal receipt/checkpoint correlation and immutable-spec verification; bounded stable step keys; malformed consumed definitions/tokens and native names; immediate persistence of an asynchronous created ID. Added a receipt-budget regression RED→GREEN and pre-rendered transport validation. Latest focused unit/client/credential run: 41 passed. Hosted receipt probe rejected 14 corrupted receipts across two deployments; five hosted/Temporal tests passed.

Ruling: use the existing common signed, digest-pinned executor image, with explicit per-pool kind admission, rather than a separate Fabric-only image. Cost: a larger dependency surface/image; tenant VM isolation and image trust remain operator responsibilities. Generated templates are in templates/fabric-solution and templates/fabric-iq.

Ruling: pre-reject documented elided TMDL default forms lexically rather than bundle an unqualified full TMDL parser. Cost: some otherwise valid native definitions require canonical source edits; undocumented normalization remains unqualified until live acceptance.

Compatibility: the new immutable spec is included only in Fabric observation/verification job requests. A regression first failed with the extra field, then passed after preserving receipt-only Databricks/Snowflake job shapes for older pinned workers.

Final: minor (deferred): the opt-in live harness deploys the same scaffold twice. It checks identity and execution, but cannot prove changed-source native updates or live cross-workspace promotion. Fake-boundary tests do exercise changed source and promotion. Adapter maturity therefore remains experimental.

Final: Ruling: actual Entra authentication, native capacity/permissions and IQ rollout require an authorized live tenant — remain explicitly unqualified — cost if wrong: account-specific deployment failure.

Final: Ruling: full TMDL semantics and undocumented enrichment require the native service or authoritative parser — preserve fail-closed readback and experimental status — cost if wrong: accepted native definitions may require manual reconciliation.

Final: Ruling: external administrators can race native mutations — no solution-wide atomic CAS is advertised; require one writer — cost if wrong: drift/partial deployment and retained claims.

Final: Ruling: automatic restart after lost acceptance remains outside the recovery profile — persist accepted operation IDs and proven item IDs, reconcile read-only — cost if wrong: manual operator recovery rather than automatic progress.

Final: Ruling: compromised signed workers, intentional tenant namespace squatting and exhaustive malformed-input security qualification remain outside this increment — operator-managed tenant identities/pools and workspace allocation are trusted — cost if wrong: tenant disruption or native privilege misuse; this is not a public-SaaS security certification.

Final: Ruling: Snowflake production behavior and cloud VM rollout are separate increments — preserve existing code and maturity; no cloud deployment/signing/publication was performed — cost if wrong: additional platform qualification/operations work remains.

Verification: final non-container regression passed 165 tests, skipped three opt-in live tests; five rebuilt-image container tests passed, including both Fabric profiles and shared confinement/timeout/build paths. TypeScript seven tests, SDK/example type checks/build, Ruff and mypy passed; all 11 Python wheels built. Production image: shifde-executor@sha256:7471e34922896a42bf58277e51942c9a3be0784ecf66f2b88882d15342166d86. Contract determinism and the separately bounded pre-existing Snowflake container harness are being finalized before commit.

Final verification: contract/schema/TypeScript exports regenerated with identical hashes. The Snowflake container lifecycle passed separately (132.36 seconds) after aligning its native job/operation deadlines inside the driver budget; the earlier stall did not reproduce in that bounded run. Complete disjoint Python partitions: 165 non-container passes plus six container passes = 171 passed, three live tests skipped. All collected tests were exercised across those partitions; no container test was quarantined or disabled. Live native qualification remains open.

Ruling: preserve and commit the inherited Snowflake implementation with the Fabric increment, since the shared contracts, dispatch and image now cover both and the prior uncommitted changes were part of the user's authorized plan. Cost: the local commit is broader than Fabric alone; no remote push or merge is performed.

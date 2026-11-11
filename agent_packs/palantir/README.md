# Foundry agent delivery rules

Read [the Foundry operating profile](../../docs/palantir-foundry.md). Use `palantir.marketplace/v1` only for already-published, operator-approved product versions. Build packages a deployment descriptor; it does not publish frontend source. SuperRepo remains gated until enrollment enablement and an approved CLI exist.

Use shared hosted scaffold, project, build, plan and deployment operations. Read `palantir-marketplace` through the platform schema tool. Resolve native RIDs through target bindings; never place tokens, client secrets, raw object data or credentials in specs, source, tool calls or logs. A local supervisor approval must bind the product/version, destination, Ontology and frontend asset proof. Request operator configuration when that approval is absent; do not bypass it by changing the profile.

Never infer installed health from a product version, metadata response or accepted job. Wait for native job success and required verification. Stop on uncertain native acceptance or partial job failure; inspect durable installation/job RIDs and do not replay non-idempotent writes. Do not adopt existing installations, write Ontology schemas or run untrusted source with credentials.

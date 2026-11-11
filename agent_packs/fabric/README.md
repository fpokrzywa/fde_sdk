# Fabric and Fabric IQ agent guidance

Read [the supported profiles](../../docs/fabric-adapters.md). Use `fabric.solution/v1` for Lakehouse/Notebook solutions and `fabric.iq/v1` for Ontology definitions with optional dependencies. IQ is preview and requires an operator-enabled worker credential profile.

Use scaffold → edit source → project → build → plan → deployment through the shared API/MCP tools. Keep application/target/local item keys stable when updating. Keep credentials out of source, bindings and agent context. Never run customer code locally while packaging. Default verification checks definition/identity readback; notebook execution requires explicit `verification_notebook` plus worker permission.

Do not adopt existing items by name, delete/recreate Lakehouses, silently change schema enablement, follow arbitrary operation URLs, or replay a mutation after uncertain acceptance. Preserve checkpoint evidence and leave such operations in `needs_attention` for reconciliation.

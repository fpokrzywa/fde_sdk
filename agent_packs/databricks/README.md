# Databricks App authoring

Use `databricks.app/v1` for the implemented Python app template. Author application code under `app/`; preserve managed bundle/runtime configuration. Add dependencies through the pinned requirements file. Do not embed workspace credentials. The Databricks runtime supplies app identity and the SQL warehouse resource resolves through `valueFrom`.

Build before planning, inspect the plan, and submit the same artifact/plan pair. Stable application and target IDs preserve native ownership. A deployment receipt is not a health claim: observe the new native deployment ID and verify the runtime warehouse check. Escalate ambiguous outcomes; do not create another plan to bypass unresolved work. Source-controlled bundle hooks, arbitrary resource definitions, implicit adoption, delete and force-lock operations are unsupported.

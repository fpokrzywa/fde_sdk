# Snowflake Native App agent guidance

Use `snowflake.native-app/v1` and the shared HTTP/MCP scaffold → project → build → plan → deploy operations. Poll operations through completion. Package upload or version creation alone does not establish an installed, healthy application.

Read [the supported profile and worker configuration](../../docs/snowflake-native-app.md). Edit generated UI/assets only; managed manifest, setup SQL and environment must stay unchanged. Use a new immutable version/patch for changed source. Keep target/application IDs stable to upgrade in place. Never introduce CLI project hooks or force replacement.

Resolve secrets only on the execution host. An agent must never put credentials into source, prompts, target bindings or API requests. Leave ambiguous native acceptance in `needs_attention`; do not bypass it with a new deployment or native deletion.

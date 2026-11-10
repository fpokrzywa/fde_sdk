# Hosted service boundaries

`api` exposes FastAPI `/v1` endpoints; `control_worker` runs Temporal coordination, execution activities and the PostgreSQL outbox dispatcher; `mcp` exposes the same application service through authenticated Streamable HTTP or an explicit caller-token stdio bridge. See [hosted development](../docs/hosted-development.md) for configuration and commands.

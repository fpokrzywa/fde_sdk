# Hosted service boundaries

The planned API, Temporal control worker, MCP facade and CLI belong here. They are not part of the first local SDK increment. Reuse `PlatformAdapter`, the canonical contracts and shared planning policy when implementing those services; do not expose local filesystem state as a multi-tenant API.

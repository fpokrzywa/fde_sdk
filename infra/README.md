# Infrastructure boundary

`compose.dev.yml` starts loopback PostgreSQL and Temporal development dependencies with persistent volumes. API and worker processes run from the uv workspace. See [hosted development](../docs/hosted-development.md). Cloud infrastructure, customer private-worker provisioning and production release pipelines remain separate milestones.

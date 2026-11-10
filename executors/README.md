# Executor boundary

`shifde-executor` is the outbound supervisor for hosted/private pools. It launches a fresh bounded container per adapter action and mediates scoped artifacts, checkpoints and local credential grants. Workers have no PostgreSQL or Temporal access. See [executor operations](../docs/executor-operations.md) for enrollment, dedicated-VM/image-signature requirements, configuration, failure recovery and real-container tests. The Databricks build remains source-only; arbitrary hostile-code compilation is not enabled.

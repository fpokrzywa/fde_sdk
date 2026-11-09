# Executor boundary

Isolated hosted jobs and outbound-only private workers are the next execution layer. The current `LocalDeliveryService` is for a trusted operator. It packages source without running it and invokes a pinned CLI with regenerated app-only configuration. It does not provide a container or VM isolation boundary for hostile code.

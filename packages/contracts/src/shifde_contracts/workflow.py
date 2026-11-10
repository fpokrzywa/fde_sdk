from dataclasses import dataclass


@dataclass(frozen=True)
class OperationRef:
    """Only opaque IDs and coordination settings enter Temporal history."""

    tenant_id: str
    operation_id: str
    resume: bool = False
    execution_queue: str = "shifde-execution"
    poll_interval: float = 2

from typing import Literal

from pydantic import Field

from shifde_contracts.common import Model, Timestamp, utcnow


class Capability(Model):
    operation: str
    status: Literal["supported", "unsupported", "unknown"]
    reason: str


class CapabilityReport(Model):
    adapter_version: str
    maturity: Literal["experimental", "supported"] = "experimental"
    application_kinds: list[str] = Field(default_factory=list)
    capabilities: list[Capability]
    captured_at: Timestamp = Field(default_factory=utcnow)
    expires_at: Timestamp

    def supports(self, operation: str) -> bool:
        return self.expires_at > utcnow() and any(
            item.operation == operation and item.status == "supported" for item in self.capabilities
        )

from pydantic import Field

from shifde_contracts.common import Digest, Identifier, Model, ResourceBinding


class NativeStepReceipt(Model):
    step_key: Identifier
    plan_digest: Digest
    resources: list[ResourceBinding] = Field(default_factory=list)
    handles: dict[str, str] = Field(default_factory=dict)

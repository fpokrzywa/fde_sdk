from typing import Annotated, Literal

from pydantic import Field, JsonValue

from shifde_contracts.builds import BuildArtifact
from shifde_contracts.common import (
    Diagnostic,
    Entity,
    Model,
    SourceArchive,
    Timestamp,
    ValidationReport,
    utcnow,
)
from shifde_contracts.execution import Deployment
from shifde_contracts.plans import DeploymentPlan, EnvironmentSnapshot

OperationState = Literal[
    "queued",
    "running",
    "waiting_for_policy",
    "reconciling",
    "needs_attention",
    "succeeded",
    "failed",
    "cancelled",
]


class BuildResult(Model):
    result_kind: Literal["build"] = "build"
    value: BuildArtifact


class PlanResult(Model):
    result_kind: Literal["plan"] = "plan"
    value: DeploymentPlan


class DeploymentResult(Model):
    result_kind: Literal["deployment"] = "deployment"
    value: Deployment


class ScaffoldResult(Model):
    result_kind: Literal["scaffold"] = "scaffold"
    value: SourceArchive


class ValidationResult(Model):
    result_kind: Literal["validation"] = "validation"
    value: ValidationReport


class InspectionResult(Model):
    result_kind: Literal["inspection"] = "inspection"
    value: EnvironmentSnapshot


OperationResult = Annotated[
    BuildResult
    | PlanResult
    | DeploymentResult
    | ScaffoldResult
    | ValidationResult
    | InspectionResult,
    Field(discriminator="result_kind"),
]


class Operation(Entity):
    operation_kind: Literal["inspection", "scaffold", "validation", "build", "plan", "deployment"]
    state: OperationState = "queued"
    created_at: Timestamp = Field(default_factory=utcnow)
    result: OperationResult | None = None
    error: Diagnostic | None = None

    def transition(self, state: OperationState) -> "Operation":
        allowed: dict[str, set[str]] = {
            "queued": {"running", "cancelled"},
            "running": {
                "waiting_for_policy",
                "reconciling",
                "needs_attention",
                "succeeded",
                "failed",
                "cancelled",
            },
            "waiting_for_policy": {"running", "failed", "cancelled"},
            "reconciling": {"running", "needs_attention", "failed", "cancelled"},
            "needs_attention": {"reconciling", "cancelled"},
        }
        if state not in allowed.get(self.state, set()):
            raise ValueError(f"invalid operation transition: {self.state} -> {state}")
        return self.model_copy(update={"state": state})


class ErrorDetail(Model):
    code: str
    message: str
    details: list[JsonValue] = Field(default_factory=list)
    trace_id: str


class ErrorEnvelope(Model):
    error: ErrorDetail

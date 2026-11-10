from pydantic import BaseModel
from shifde_contracts.builds import BuildArtifact, BuildRequest, ScaffoldRequest, ValidateRequest
from shifde_contracts.capabilities import CapabilityReport
from shifde_contracts.common import SourceArchive, ValidationReport, VerificationReport
from shifde_contracts.execution import (
    ApplyReceipt,
    ApplyRequest,
    NativeObservation,
    ObserveRequest,
    ReconcileRequest,
    ReconciliationResult,
    VerifyRequest,
)
from shifde_contracts.plans import DeploymentPlan, EnvironmentSnapshot, InspectRequest, PlanRequest

REQUESTS: dict[str, type[BaseModel]] = {
    "scaffold": ScaffoldRequest,
    "validate": ValidateRequest,
    "build": BuildRequest,
    "plan": PlanRequest,
    "apply": ApplyRequest,
    "inspect": InspectRequest,
    "observe": ObserveRequest,
    "verify": VerifyRequest,
    "reconcile": ReconcileRequest,
}
RESULTS: dict[str, type[BaseModel]] = {
    "scaffold": SourceArchive,
    "validate": ValidationReport,
    "build": BuildArtifact,
    "capabilities": CapabilityReport,
    "inspect": EnvironmentSnapshot,
    "plan": DeploymentPlan,
    "apply": ApplyReceipt,
    "observe": NativeObservation,
    "verify": VerificationReport,
    "reconcile": ReconciliationResult,
}
OFFLINE = {"build", "validate", "scaffold"}


def input_archives(request: dict) -> list[SourceArchive]:
    if "source" in request:
        return [SourceArchive.model_validate(request["source"])]
    if "artifact" in request:
        return [SourceArchive.model_validate(request["artifact"]["archive"])]
    return []

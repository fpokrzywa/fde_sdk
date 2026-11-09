"""Export canonical Pydantic models; run from the repository root."""

import json
from pathlib import Path
from typing import Any, cast

from pydantic import TypeAdapter
from shifde_contracts.builds import BuildArtifact
from shifde_contracts.common import Target
from shifde_contracts.execution import ApplyReceipt, Deployment
from shifde_contracts.operations import ErrorEnvelope, Operation
from shifde_contracts.plans import DeploymentPlan
from shifde_contracts.projects import ApplicationSpec, Project
from shifde_contracts.submissions import BuildSubmission, DeploymentSubmission, PlanSubmission

ROOT = Path(__file__).resolve().parents[1]


def export_contracts(output_dir: Path = ROOT / "schemas") -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    union = TypeAdapter[Any](
        cast(
            Any,
            Operation
            | BuildArtifact
            | DeploymentPlan
            | Deployment
            | ApplyReceipt
            | Project
            | Target
            | ErrorEnvelope
            | BuildSubmission
            | PlanSubmission
            | DeploymentSubmission,
        )
    )
    schema = union.json_schema(ref_template="#/$defs/{model}", mode="serialization")
    schema.update(
        title="ShifdeContracts", **{"$schema": "https://json-schema.org/draft/2020-12/schema"}
    )
    (output_dir / "contracts.json").write_text(
        json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    native = TypeAdapter[ApplicationSpec](cast(Any, ApplicationSpec)).json_schema()
    (output_dir / "application-specs.json").write_text(
        json.dumps(native, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )


if __name__ == "__main__":
    export_contracts()

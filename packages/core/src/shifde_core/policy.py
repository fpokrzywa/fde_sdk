from shifde_contracts.plans import PlanStep


def validate_changes(steps: list[PlanStep]) -> None:
    if not steps:
        raise ValueError("deployment plan must contain steps")
    if any(step.action in ("delete", "unknown") for step in steps):
        raise PermissionError("default policy forbids deletion and unknown effects")

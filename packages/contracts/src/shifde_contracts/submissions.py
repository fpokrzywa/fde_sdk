from uuid import UUID

from shifde_contracts.common import Model


class BuildSubmission(Model):
    project_id: UUID


class PlanSubmission(Model):
    artifact_id: UUID
    target_id: UUID


class DeploymentSubmission(Model):
    plan_id: UUID
    artifact_id: UUID

import {ShifdeClient} from "@shifde/sdk";

// This boundary targets the future hosted API; local delivery uses LocalDeliveryService.
export async function deliverNativeApp(
  client: ShifdeClient,
  projectId: string,
  targetId: string,
  buildRequestKey: string,
  deployRequestKey: string,
) {
  const build = await client.builds.create({project_id: projectId}, {idempotencyKey: buildRequestKey});
  const artifact = await client.operations.waitForResult(build.id, {expectedKind: "build"});
  const planning = await client.plans.create({artifact_id: artifact.id, target_id: targetId});
  const plan = await client.operations.waitForResult(planning.id, {expectedKind: "plan"});
  const deployment = await client.deployments.create({plan_id: plan.id, artifact_id: artifact.id}, {idempotencyKey: deployRequestKey});
  return client.operations.waitForResult(deployment.id, {expectedKind: "deployment"});
}

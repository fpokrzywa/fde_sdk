/* Generated from Pydantic contracts. Run pnpm generate; do not edit. */

export type ShifdeContracts =
  | Operation
  | BuildArtifact
  | DeploymentPlan
  | Deployment
  | ApplyReceipt
  | Project
  | Target
  | ErrorEnvelope
  | BuildSubmission
  | PlanSubmission
  | DeploymentSubmission
  | ApplicationSubmission
  | ConnectionSubmission
  | Connection
  | TargetSubmission
  | TargetPatch
  | ProjectSubmission
  | SourceUploadSubmission
  | SourceUpload
  | SourceUploadCompletion
  | ScaffoldSubmission
  | InspectionSubmission
  | ValidationSubmission
  | OperationControl
  | PolicyDecisionSubmission
  | EventPage
  | DeploymentView
  | WorkerPool
  | ExecutionJob
  | JobLease
  | LeaseProof
  | LeaseRequest
  | CheckpointSubmission
  | JobResultSubmission
  | CredentialGrant;
export type CreatedAt = string;
export type Code = string;
export type Message = string;
export type Path = string | null;
export type Id = string;
export type OperationKind = "inspection" | "scaffold" | "validation" | "build" | "plan" | "deployment";
export type Result =
  (BuildResult | PlanResult | DeploymentResult | ScaffoldResult | ValidationResult | InspectionResult) | null;
export type ResultKind = "build";
export type AdapterVersion = string;
export type ApplicationId = string;
export type Digest = string;
export type Format = string;
export type Id1 = string;
export type Size = number;
export type TenantId = string;
export type Id2 = string;
export type SourceDigest = string;
export type Spec =
  DatabricksApplicationSpec | FoundryApplicationSpec | SnowflakeApplicationSpec | FabricApplicationSpec;
export type AppResourceKey = string;
export type BundleRoot = string;
export type BundleTarget = string;
export type HealthPath = "/health";
export type Kind = "databricks.app/v1";
export type WarehouseBinding = string;
export type FrontendPath = string;
export type Kind1 = "palantir.superrepo/v1";
export type OntologyBinding = string;
export type OsdkPackage = string;
export type ProductBinding = string;
export type SuperrepoRoot = string;
export type ApplicationBinding = string;
export type Kind2 = "snowflake.native-app/v1";
export type PackageBinding = string;
export type ProjectRoot = string;
export type ReleasePatch = number;
export type ReleaseVersion = string;
export type WarehouseBinding1 = string;
/**
 * @minItems 1
 */
export type Items = [FabricItem, ...FabricItem[]];
export type DefinitionPath = string;
export type DependsOn = string[];
export type ItemType = "Lakehouse" | "Notebook";
export type Key = string;
export type Kind3 = "fabric.solution/v1";
export type SolutionRoot = string;
export type SpecDigest = string;
export type TemplateVersion = string;
export type TenantId1 = string;
export type ToolchainDigest = string;
export type ResultKind1 = "plan";
export type AdapterVersion1 = string;
export type ApplicationId1 = string;
export type ArtifactDigest = string;
export type ArtifactId = string;
export type BindingsDigest = string;
export type ConnectionRevision = number;
export type CreatedAt1 = string;
export type JsonValue = unknown;
export type Digest1 = string;
export type ExpiresAt = string;
export type Id3 = string;
export type CapturedAt = string;
export type Fingerprint = string;
export type ApplicationId2 = string;
export type Key1 = string;
export type NativeId = string;
export type ResourceType = string;
export type TargetId = string;
export type Resources = ResourceBinding[];
export type SpecDigest1 = string;
export type Action = "create" | "update" | "start" | "delete" | "unknown";
export type Key2 = string;
export type ResourceKey = string;
export type Reversible = boolean;
export type Steps = PlanStep[];
export type TargetId1 = string;
export type TenantId2 = string;
export type ToolchainDigest1 = string;
export type ResultKind2 = "deployment";
export type ApplicationId3 = string;
export type ArtifactDigest1 = string;
export type Healthy = boolean;
export type Id4 = string;
export type Resources1 = ResourceBinding[];
export type TargetId2 = string;
export type TenantId3 = string;
export type ResultKind3 = "scaffold";
export type ResultKind4 = "validation";
export type Diagnostics = Diagnostic[];
export type Passed = boolean;
export type ResultKind5 = "inspection";
export type State =
  | "queued"
  | "running"
  | "waiting_for_policy"
  | "reconciling"
  | "needs_attention"
  | "succeeded"
  | "failed"
  | "cancelled";
export type TenantId4 = string;
export type ApplicationId4 = string;
export type ArtifactDigest2 = string;
export type Id5 = string;
export type Resources2 = ResourceBinding[];
export type TargetId3 = string;
export type TenantId5 = string;
export type ApplicationId5 = string;
export type Id6 = string;
export type Revision = number;
export type Spec1 =
  DatabricksApplicationSpec | FoundryApplicationSpec | SnowflakeApplicationSpec | FabricApplicationSpec;
export type TenantId6 = string;
export type ConnectionId = string;
export type ConnectionRevision1 = number;
export type Environment = string;
export type Host = string;
export type Id7 = string;
export type TenantId7 = string;
export type Code1 = string;
export type Details1 = JsonValue[];
export type Message1 = string;
export type TraceId = string;
export type ProjectId = string;
export type ArtifactId1 = string;
export type TargetId4 = string;
export type ArtifactId2 = string;
export type PlanId = string;
export type Kind4 = "databricks.app/v1" | "palantir.superrepo/v1" | "snowflake.native-app/v1" | "fabric.solution/v1";
export type Name = string;
export type CredentialRef = string;
export type Host1 = string;
export type Name1 = string;
export type CredentialRef1 = string;
export type Host2 = string;
export type Id8 = string;
export type Name2 = string;
export type Revision1 = number;
export type TenantId8 = string;
export type ConnectionId1 = string;
export type Environment1 = string;
export type Bindings3 = {
  [k: string]: string;
} | null;
export type Environment2 = string | null;
export type ApplicationId6 = string;
export type SourceId = string;
export type Spec2 =
  DatabricksApplicationSpec | FoundryApplicationSpec | SnowflakeApplicationSpec | FabricApplicationSpec;
export type Digest2 = string;
export type Size1 = number;
export type Digest3 = string;
export type ExpiresAt1 = string;
export type Id9 = string;
export type Size2 = number;
export type TenantId9 = string;
export type UploadId = string;
export type ApplicationId7 = string;
export type Spec3 =
  DatabricksApplicationSpec | FoundryApplicationSpec | SnowflakeApplicationSpec | FabricApplicationSpec;
export type TemplateVersion1 = string;
export type ApplicationId8 = string;
export type TargetId5 = string;
export type ProjectId1 = string;
export type OperationId = string;
export type Approve = boolean;
export type OperationId1 = string;
export type PlanDigest = string;
export type CreatedAt2 = string;
export type Name3 = string;
export type Seq = number;
export type Data = OperationEvent[];
export type NextCursor = number;
/**
 * @minItems 1
 */
export type Actions = [
  (
    | "capabilities"
    | "inspect"
    | "scaffold"
    | "validate"
    | "build"
    | "plan"
    | "apply"
    | "observe"
    | "reconcile"
    | "verify"
  ),
  ...(
    | "capabilities"
    | "inspect"
    | "scaffold"
    | "validate"
    | "build"
    | "plan"
    | "apply"
    | "observe"
    | "reconcile"
    | "verify"
  )[]
];
export type ConnectionIds = string[];
export type Enabled = boolean;
export type Id10 = string;
export type Image = string;
export type Mode = "hosted" | "private";
export type Name4 = string;
export type TenantId10 = string;
export type Action1 =
  | "capabilities"
  | "inspect"
  | "scaffold"
  | "validate"
  | "build"
  | "plan"
  | "apply"
  | "observe"
  | "reconcile"
  | "verify";
export type CredentialRef2 = string | null;
export type Deadline = string;
export type ExecutionId = string;
export type Id11 = string;
export type Image1 = string;
export type Inputs = SourceArchive[];
export type OperationFence = number;
export type OperationId2 = string;
export type OwnedResources = ResourceBinding[];
export type PoolId = string;
export type TenantId11 = string;
export type ToolchainDigest2 = string;
export type ExpiresAt2 = string;
export type Fence = number;
export type LeaseId = string;
export type RequestId = string;
export type Fence1 = number;
export type LeaseId1 = string;
export type Image2 = string;
export type RequestId1 = string;
export type Fence2 = number;
export type LeaseId2 = string;
export type Phase = "intent" | "complete";
export type Sequence = number;
export type Step = string;
export type ErrorCode = ("EXECUTION_FAILED" | "CREDENTIAL_UNAVAILABLE" | "SANDBOX_FAILED") | null;
export type Fence3 = number;
export type LeaseId3 = string;
export type Status = "succeeded" | "failed";
export type Value = {
  [k: string]: unknown;
} | null;
export type Audience = string;
export type CredentialRef3 = string;
export type ExpiresAt3 = string;
export type JobId = string;
export type PoolId1 = string;
export type TargetId6 = string;
export type TenantId12 = string;
export type WorkerId = string;

export interface Operation {
  created_at: CreatedAt;
  error: Diagnostic | null;
  id: Id;
  operation_kind: OperationKind;
  result: Result;
  state: State;
  tenant_id: TenantId4;
}
export interface Diagnostic {
  code: Code;
  message: Message;
  path: Path;
}
export interface BuildResult {
  result_kind: ResultKind;
  value: BuildArtifact;
}
export interface BuildArtifact {
  adapter_version: AdapterVersion;
  application_id: ApplicationId;
  archive: SourceArchive;
  id: Id2;
  source_digest: SourceDigest;
  spec: Spec;
  spec_digest: SpecDigest;
  template_version: TemplateVersion;
  tenant_id: TenantId1;
  toolchain_digest: ToolchainDigest;
}
export interface SourceArchive {
  digest: Digest;
  format: Format;
  id: Id1;
  size: Size;
  tenant_id: TenantId;
}
export interface DatabricksApplicationSpec {
  app_resource_key: AppResourceKey;
  bundle_root: BundleRoot;
  bundle_target: BundleTarget;
  health_path: HealthPath;
  kind: Kind;
  warehouse_binding: WarehouseBinding;
}
export interface FoundryApplicationSpec {
  frontend_path: FrontendPath;
  kind: Kind1;
  ontology_binding: OntologyBinding;
  osdk_package: OsdkPackage;
  product_binding: ProductBinding;
  superrepo_root: SuperrepoRoot;
}
export interface SnowflakeApplicationSpec {
  application_binding: ApplicationBinding;
  kind: Kind2;
  package_binding: PackageBinding;
  project_root: ProjectRoot;
  release_patch: ReleasePatch;
  release_version: ReleaseVersion;
  warehouse_binding: WarehouseBinding1;
}
export interface FabricApplicationSpec {
  bindings: Bindings;
  items: Items;
  kind: Kind3;
  solution_root: SolutionRoot;
}
export interface Bindings {
  [k: string]: string;
}
export interface FabricItem {
  definition_path: DefinitionPath;
  depends_on: DependsOn;
  item_type: ItemType;
  key: Key;
}
export interface PlanResult {
  result_kind: ResultKind1;
  value: DeploymentPlan;
}
export interface DeploymentPlan {
  adapter_version: AdapterVersion1;
  application_id: ApplicationId1;
  artifact_digest: ArtifactDigest;
  artifact_id: ArtifactId;
  bindings_digest: BindingsDigest;
  connection_revision: ConnectionRevision;
  created_at: CreatedAt1;
  details: Details;
  digest: Digest1;
  expires_at: ExpiresAt;
  id: Id3;
  snapshot: EnvironmentSnapshot;
  spec_digest: SpecDigest1;
  steps: Steps;
  target_id: TargetId1;
  tenant_id: TenantId2;
  toolchain_digest: ToolchainDigest1;
}
export interface Details {
  [k: string]: JsonValue;
}
export interface EnvironmentSnapshot {
  captured_at: CapturedAt;
  fingerprint: Fingerprint;
  resources: Resources;
}
export interface ResourceBinding {
  application_id: ApplicationId2;
  key: Key1;
  native_id: NativeId;
  resource_type: ResourceType;
  target_id: TargetId;
}
export interface PlanStep {
  action: Action;
  key: Key2;
  resource_key: ResourceKey;
  reversible: Reversible;
}
export interface DeploymentResult {
  result_kind: ResultKind2;
  value: Deployment;
}
export interface Deployment {
  application_id: ApplicationId3;
  artifact_digest: ArtifactDigest1;
  healthy: Healthy;
  id: Id4;
  resources: Resources1;
  target_id: TargetId2;
  tenant_id: TenantId3;
}
export interface ScaffoldResult {
  result_kind: ResultKind3;
  value: SourceArchive;
}
export interface ValidationResult {
  result_kind: ResultKind4;
  value: ValidationReport;
}
export interface ValidationReport {
  diagnostics: Diagnostics;
  passed: Passed;
}
export interface InspectionResult {
  result_kind: ResultKind5;
  value: EnvironmentSnapshot;
}
export interface ApplyReceipt {
  application_id: ApplicationId4;
  artifact_digest: ArtifactDigest2;
  id: Id5;
  operation_handles: OperationHandles;
  resources: Resources2;
  target_id: TargetId3;
  tenant_id: TenantId5;
}
export interface OperationHandles {
  [k: string]: string;
}
export interface Project {
  application_id: ApplicationId5;
  id: Id6;
  revision: Revision;
  source: SourceArchive;
  spec: Spec1;
  tenant_id: TenantId6;
}
export interface Target {
  bindings: Bindings1;
  connection_id: ConnectionId;
  connection_revision: ConnectionRevision1;
  environment: Environment;
  host: Host;
  id: Id7;
  tenant_id: TenantId7;
}
export interface Bindings1 {
  [k: string]: string;
}
export interface ErrorEnvelope {
  error: ErrorDetail;
}
export interface ErrorDetail {
  code: Code1;
  details: Details1;
  message: Message1;
  trace_id: TraceId;
}
export interface BuildSubmission {
  project_id: ProjectId;
}
export interface PlanSubmission {
  artifact_id: ArtifactId1;
  target_id: TargetId4;
}
export interface DeploymentSubmission {
  artifact_id: ArtifactId2;
  plan_id: PlanId;
}
export interface ApplicationSubmission {
  kind: Kind4;
  name: Name;
}
export interface ConnectionSubmission {
  credential_ref: CredentialRef;
  host: Host1;
  name: Name1;
}
export interface Connection {
  credential_ref: CredentialRef1;
  host: Host2;
  id: Id8;
  name: Name2;
  revision: Revision1;
  tenant_id: TenantId8;
}
export interface TargetSubmission {
  bindings: Bindings2;
  connection_id: ConnectionId1;
  environment: Environment1;
}
export interface Bindings2 {
  [k: string]: string;
}
export interface TargetPatch {
  bindings: Bindings3;
  environment: Environment2;
}
export interface ProjectSubmission {
  application_id: ApplicationId6;
  source_id: SourceId;
  spec: Spec2;
}
export interface SourceUploadSubmission {
  digest: Digest2;
  size: Size1;
}
export interface SourceUpload {
  digest: Digest3;
  expires_at: ExpiresAt1;
  id: Id9;
  size: Size2;
  source: SourceArchive | null;
  tenant_id: TenantId9;
}
export interface SourceUploadCompletion {
  upload_id: UploadId;
}
export interface ScaffoldSubmission {
  application_id: ApplicationId7;
  spec: Spec3;
  template_version: TemplateVersion1;
}
export interface InspectionSubmission {
  application_id: ApplicationId8;
  target_id: TargetId5;
}
export interface ValidationSubmission {
  project_id: ProjectId1;
}
export interface OperationControl {
  operation_id: OperationId;
}
export interface PolicyDecisionSubmission {
  approve: Approve;
  operation_id: OperationId1;
  plan_digest: PlanDigest;
}
export interface EventPage {
  data: Data;
  next_cursor: NextCursor;
}
export interface OperationEvent {
  created_at: CreatedAt2;
  data: Data1;
  name: Name3;
  seq: Seq;
}
export interface Data1 {
  [k: string]: JsonValue;
}
export interface DeploymentView {
  deployment: Deployment;
  operation: Operation;
}
export interface WorkerPool {
  actions: Actions;
  connection_ids: ConnectionIds;
  enabled: Enabled;
  id: Id10;
  image: Image;
  mode: Mode;
  name: Name4;
  tenant_id: TenantId10;
}
export interface ExecutionJob {
  action: Action1;
  credential_ref: CredentialRef2;
  deadline: Deadline;
  execution_id: ExecutionId;
  id: Id11;
  image: Image1;
  inputs: Inputs;
  journal: Journal;
  operation_fence: OperationFence;
  operation_id: OperationId2;
  owned_resources: OwnedResources;
  pool_id: PoolId;
  request: Request;
  target: Target | null;
  tenant_id: TenantId11;
  toolchain_digest: ToolchainDigest2;
}
export interface Journal {
  [k: string]: {
    [k: string]: unknown;
  };
}
export interface Request {
  [k: string]: unknown;
}
export interface JobLease {
  expires_at: ExpiresAt2;
  fence: Fence;
  job: ExecutionJob;
  lease_id: LeaseId;
  request_id: RequestId;
}
export interface LeaseProof {
  fence: Fence1;
  lease_id: LeaseId1;
}
export interface LeaseRequest {
  image: Image2;
  request_id: RequestId1;
}
export interface CheckpointSubmission {
  data: Data2;
  fence: Fence2;
  lease_id: LeaseId2;
  phase: Phase;
  sequence: Sequence;
  step: Step;
}
export interface Data2 {
  [k: string]: unknown;
}
export interface JobResultSubmission {
  error_code: ErrorCode;
  fence: Fence3;
  lease_id: LeaseId3;
  status: Status;
  value: Value;
}
export interface CredentialGrant {
  audience: Audience;
  credential_ref: CredentialRef3;
  expires_at: ExpiresAt3;
  job_id: JobId;
  pool_id: PoolId1;
  target_id: TargetId6;
  tenant_id: TenantId12;
  worker_id: WorkerId;
}

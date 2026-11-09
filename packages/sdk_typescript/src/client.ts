import Ajv2020 from "ajv/dist/2020.js";
import addFormats from "ajv-formats";
import {schema} from "./generated/schema.js";
import type {BuildArtifact, BuildSubmission, Deployment, DeploymentPlan, DeploymentSubmission, Operation, PlanSubmission} from "./generated/contracts.js";

// Pydantic is canonical; Ajv validates the exported schema at the network boundary.
const ajv = new Ajv2020.default({strict: false, allErrors: false});
addFormats.default(ajv);
ajv.addSchema(schema, "contracts");
const validOperation = ajv.compile({$ref: "contracts#/$defs/Operation"});
const submissions = {
  builds: ajv.compile({$ref: "contracts#/$defs/BuildSubmission"}),
  plans: ajv.compile({$ref: "contracts#/$defs/PlanSubmission"}),
  deployments: ajv.compile({$ref: "contracts#/$defs/DeploymentSubmission"}),
};

export class ApiError extends Error {
  constructor(public readonly status: number, public readonly code = "REQUEST_FAILED") {
    super(`delivery API returned HTTP ${status} (${code})`);
    this.name = "ApiError";
  }
}

export class OperationAttentionRequired extends Error {
  constructor(public readonly operation: Operation) {
    super(`operation ${operation.id} requires attention: ${operation.state}`);
    this.name = "OperationAttentionRequired";
  }
}

export class OperationFailed extends Error {
  constructor(public readonly operation: Operation) {
    super(`operation ${operation.id} is ${operation.state}`);
    this.name = "OperationFailed";
  }
}

interface ClientOptions {
  baseUrl: string;
  tokenProvider: () => string | Promise<string>;
  fetch?: typeof globalThis.fetch;
  requestTimeoutMs?: number;
}

interface RequestOptions {
  idempotencyKey?: string;
  signal?: AbortSignal;
}

interface ResultMap {build: BuildArtifact; plan: DeploymentPlan; deployment: Deployment}

interface WaitOptions<K extends keyof ResultMap> {
  expectedKind: K;
  timeoutMs?: number;
  pollIntervalMs?: number;
  signal?: AbortSignal;
}

function uuid(value: string): string {
  if (!/^[a-f\d]{8}-[a-f\d]{4}-[a-f\d]{4}-[a-f\d]{4}-[a-f\d]{12}$/i.test(value)) {
    throw new Error("Expected a UUID");
  }
  return value;
}

function parseOperation(value: unknown): Operation {
  if (!validOperation(value)) throw new Error("Invalid operation response");
  return value as Operation;
}

export class ShifdeClient {
  readonly builds: {create: (request: BuildSubmission, options?: RequestOptions) => Promise<Operation>};
  readonly plans: {create: (request: PlanSubmission, options?: RequestOptions) => Promise<Operation>};
  readonly deployments: {create: (request: DeploymentSubmission, options?: RequestOptions) => Promise<Operation>};
  readonly operations: {
    get: (id: string, options?: RequestOptions) => Promise<Operation>;
    waitForResult: <K extends keyof ResultMap>(id: string, options: WaitOptions<K>) => Promise<ResultMap[K]>;
  };
  private readonly origin: string;
  private readonly fetcher: typeof globalThis.fetch;

  constructor(private readonly options: ClientOptions) {
    const url = new URL(options.baseUrl);
    if (url.protocol !== "https:" || url.username || url.password || url.search || url.hash || url.pathname !== "/") {
      throw new Error("baseUrl must be an HTTPS origin without credentials or a path");
    }
    this.origin = url.origin;
    this.fetcher = options.fetch ?? globalThis.fetch;
    const resource = <T>(name: keyof typeof submissions) => ({create: async (request: T, opts: RequestOptions = {}) => {
      if (!submissions[name](request)) throw new Error("Invalid submission");
      return parseOperation(await this.request("POST", name, request, {...opts, idempotencyKey: opts.idempotencyKey ?? crypto.randomUUID()}));
    }});
    this.builds = resource("builds");
    this.plans = resource("plans");
    this.deployments = resource("deployments");
    this.operations = {
      get: async (id, opts = {}) => parseOperation(await this.request("GET", `operations/${uuid(id)}`, undefined, opts)),
      waitForResult: (id, opts) => this.waitForResult(id, opts),
    };
  }

  private async request(method: string, path: string, body: unknown, options: RequestOptions): Promise<unknown> {
    const timeout = this.options.requestTimeoutMs ?? 30_000;
    if (timeout <= 0) throw new Error("requestTimeoutMs must be positive");
    const signal = AbortSignal.any([AbortSignal.timeout(timeout), ...(options.signal ? [options.signal] : [])]);
    // Token acquisition is part of the request deadline as well.
    const token = await this.abortable(Promise.resolve().then(this.options.tokenProvider), signal);
    if (!token || /[\r\n]/.test(token)) throw new Error("Expected a nonempty bearer token");
    const headers: Record<string, string> = {Authorization: `Bearer ${token}`};
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (options.idempotencyKey) headers["Idempotency-Key"] = options.idempotencyKey;
    const response = await this.fetcher(`${this.origin}/v1/${path}`, {method, headers,
      body: body === undefined ? undefined : JSON.stringify(body), signal, redirect: "error"});
    if (!response.ok) {
      let code = "REQUEST_FAILED";
      try {
        const value = await response.json() as {error?: {code?: string}};
        if (typeof value.error?.code === "string" && /^[A-Z0-9_]{1,80}$/.test(value.error.code)) code = value.error.code;
      } catch { /* Never expose arbitrary error response content. */ }
      throw new ApiError(response.status, code);
    }
    return response.json();
  }

  private abortable<T>(promise: Promise<T>, signal: AbortSignal): Promise<T> {
    return new Promise((resolve, reject) => {
      if (signal.aborted) { reject(signal.reason); return; }
      const abort = () => reject(signal.reason);
      signal.addEventListener("abort", abort, {once: true});
      promise.then(resolve, reject).finally(() => signal.removeEventListener("abort", abort));
    });
  }

  private async waitForResult<K extends keyof ResultMap>(id: string, options: WaitOptions<K>): Promise<ResultMap[K]> {
    const timeout = options.timeoutMs ?? 60_000;
    const interval = options.pollIntervalMs ?? 1000;
    if (timeout <= 0 || interval <= 0) throw new Error("Polling timeout and interval must be positive");
    const signal = AbortSignal.any([AbortSignal.timeout(timeout), ...(options.signal ? [options.signal] : [])]);
    while (true) {
      const operation = await this.operations.get(id, {signal});
      if (operation.operation_kind !== options.expectedKind) throw new Error("Operation kind does not match requested result");
      if (operation.state === "needs_attention" || operation.state === "waiting_for_policy") throw new OperationAttentionRequired(operation);
      if (operation.state === "failed" || operation.state === "cancelled") throw new OperationFailed(operation);
      if (operation.state === "succeeded") {
        if (!operation.result || operation.result.result_kind !== options.expectedKind) throw new Error("Operation result kind is missing or invalid");
        return operation.result.value as ResultMap[K];
      }
      await new Promise<void>((resolve, reject) => {
        if (signal.aborted) { reject(signal.reason); return; }
        const abort = () => { clearTimeout(timer); reject(signal.reason); };
        const timer = setTimeout(() => { signal.removeEventListener("abort", abort); resolve(); }, interval);
        signal.addEventListener("abort", abort, {once: true});
      });
    }
  }
}

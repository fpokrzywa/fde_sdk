"""Trusted adapter process inside a fresh sandbox. All authority goes through RPC."""

import asyncio
import base64
import hashlib
import json
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

from databricks.sdk import WorkspaceClient
from databricks.sdk.core import Config
from databricks.sdk.credentials_provider import credentials_strategy
from pydantic import BaseModel
from shifde_contracts.builds import BuildRequest, ScaffoldRequest, ValidateRequest
from shifde_contracts.common import SourceArchive
from shifde_contracts.execution import ApplyRequest, ObserveRequest, ReconcileRequest, VerifyRequest
from shifde_contracts.jobs import ExecutionJob
from shifde_contracts.plans import InspectRequest, PlanRequest
from shifde_core.artifact_store import read_archive
from shifde_core.contexts import BuildContext, DeployContext, ReadContext
from shifde_databricks.adapter import DatabricksAdapter
from shifde_databricks.clients import DatabricksCLI, DatabricksWorkspaceGateway

MAX_FRAME = 32 * 1024 * 1024
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


class RPC:
    def __init__(self):
        self.lock = threading.Lock()

    def call(self, method, data=None):
        with self.lock:
            print(json.dumps({"rpc": method, "data": data or {}}), flush=True)
            line = sys.stdin.readline(MAX_FRAME + 1)
            if len(line) > MAX_FRAME:
                raise RuntimeError("executor protocol frame exceeded")
            reply = json.loads(line)
            if not reply.get("ok"):
                raise RuntimeError("execution authority unavailable")
            return reply["value"]


class RemoteArtifacts:
    def __init__(self, job, rpc, root):
        self.job, self.rpc, self.root = job, rpc, root

    def read(self, reference, tenant_id):
        if tenant_id != self.job.tenant_id or reference not in self.job.inputs:
            raise PermissionError("artifact is not part of this job")
        data = base64.b64decode(
            self.rpc.call("read", {"source_id": str(reference.id)})["bytes"], validate=True
        )
        if len(data) != reference.size or hashlib.sha256(data).hexdigest() != reference.digest:
            raise ValueError("artifact integrity failure")
        read_archive(data)
        return data

    def put(self, tenant_id, data):
        if tenant_id != self.job.tenant_id:
            raise PermissionError("artifact tenant mismatch")
        read_archive(data)
        return SourceArchive.model_validate(
            self.rpc.call("put", {"bytes": base64.b64encode(data).decode()})
        )


class RemoteJournal:
    def __init__(self, job, rpc):
        self.tenant_id, self.operation_id = job.tenant_id, job.operation_id
        self.saved = dict(job.journal)
        self.rpc, self.sequence = rpc, 0

    def guard(self):
        self.rpc.call("guard")

    def intent(self, key, inputs):
        result = self.rpc.call(
            "checkpoint",
            {"sequence": self.sequence + 1, "phase": "intent", "step": key, "data": inputs},
        )
        self.sequence += 1
        if result["accepted"]:
            self.saved[key] = {"state": "intent", "receipt": None}
        return result["accepted"]

    def complete(self, key, receipt):
        self.rpc.call(
            "checkpoint",
            {"sequence": self.sequence + 1, "phase": "complete", "step": key, "data": receipt},
        )
        self.sequence += 1
        self.saved[key] = {"state": "complete", "receipt": receipt}

    def completed(self, key):
        saved = self.saved.get(key)
        return saved["receipt"] if saved and saved["state"] == "complete" else None

    def has_intent(self, key):
        return key in self.saved


def native_adapter(job, rpc):
    if job.application_kind in {"fabric.solution/v1", "fabric.iq/v1"}:
        from shifde_fabric.adapter import FabricAdapter, FabricIQAdapter
        from shifde_fabric.clients import FabricClient

        iq = job.application_kind == "fabric.iq/v1"
        return (FabricIQAdapter if iq else FabricAdapter)(
            client_factory=lambda target: FabricClient(
                target, lambda: rpc.call("credential"), iq=iq
            )
        )
    if job.application_kind == "snowflake.native-app/v1":
        from shifde_snowflake.adapter import SnowflakeAdapter
        from shifde_snowflake.clients import SnowflakeClient

        return SnowflakeAdapter(
            client_factory=lambda target: SnowflakeClient(
                target,
                lambda: rpc.call("credential"),
                query_tag=f"shifde:{job.operation_id}:{job.id}",
            )
        )
    if job.target is None:
        return DatabricksAdapter()

    def token(host):
        if host != job.target.host:
            raise PermissionError("credential audience mismatch")
        return rpc.call("credential")["bearer"]

    @credentials_strategy("shifde-job", [])
    def credentials(config):
        return lambda: {"Authorization": "Bearer " + token(config.host)}

    client = WorkspaceClient(
        config=Config(
            host=job.target.host,
            credentials_strategy=credentials,
            http_timeout_seconds=30,
            retry_timeout_seconds=10,
        )
    )
    return DatabricksAdapter(
        cli=DatabricksCLI(token_provider=token),
        workspace_factory=lambda target: DatabricksWorkspaceGateway(target, client=client),
    )


async def run_job(job: ExecutionJob, rpc, *, adapter=None):
    adapter = adapter or native_adapter(job, rpc)
    ctx: BuildContext | ReadContext
    with tempfile.TemporaryDirectory(prefix="shifde-job-") as root:
        artifacts = RemoteArtifacts(job, rpc, Path(root))
        if job.action in {"scaffold", "validate", "build"}:
            ctx = BuildContext(
                tenant_id=job.tenant_id,
                operation_id=job.operation_id,
                artifacts=artifacts,
                workspace=Path(root) / "workspace",
                toolchain_digest=job.toolchain_digest,
            )
        else:
            if job.target is None:
                raise ValueError("native jobs require a target")
            common: dict[str, Any] = dict(
                tenant_id=job.tenant_id,
                operation_id=job.operation_id,
                target=job.target,
                owned_resources=tuple(job.owned_resources),
                deadline=job.deadline,
            )
            if job.action == "apply":
                journal = RemoteJournal(job, rpc)
                ctx = DeployContext(
                    **common,
                    artifacts=artifacts,
                    toolchain_digest=job.toolchain_digest,
                    journal=journal,
                    write_guard=journal.guard,
                )
            else:
                ctx = ReadContext(**common)
        if job.action == "capabilities":
            value = await adapter.capabilities(ctx)
        else:
            request = REQUESTS[job.action].model_validate(job.request)
            value = await getattr(adapter, job.action)(request, ctx)
        return value.model_dump(mode="json")


def main():
    try:
        line = sys.stdin.readline(MAX_FRAME + 1)
        if len(line) > MAX_FRAME:
            raise ValueError("job too large")
        job = ExecutionJob.model_validate(json.loads(line))
        value = asyncio.run(run_job(job, RPC()))
        print(json.dumps({"result": {"status": "succeeded", "value": value}}), flush=True)
    except Exception:
        # Never print arbitrary exception text, native CLI output, source or credentials.
        print(
            json.dumps({"result": {"status": "failed", "error_code": "EXECUTION_FAILED"}}),
            flush=True,
        )


if __name__ == "__main__":
    main()

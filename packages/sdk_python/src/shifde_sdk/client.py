import asyncio
import inspect
import re
from collections.abc import Awaitable, Callable
from typing import Literal, overload
from uuid import UUID, uuid4

import httpx
from pydantic import BaseModel
from shifde_contracts.builds import BuildArtifact
from shifde_contracts.common import Target
from shifde_contracts.execution import Deployment
from shifde_contracts.operations import Operation
from shifde_contracts.plans import DeploymentPlan
from shifde_contracts.submissions import BuildSubmission, DeploymentSubmission, PlanSubmission

TokenProvider = Callable[[], str | Awaitable[str]]


class ApiError(RuntimeError):
    def __init__(self, status: int, code: str = "REQUEST_FAILED", trace_id: str | None = None):
        self.status, self.code, self.trace_id = status, code, trace_id
        super().__init__(f"delivery API returned HTTP {status} ({code})")


class OperationAttentionRequired(RuntimeError):
    def __init__(self, operation: Operation):
        self.operation = operation
        super().__init__(f"operation {operation.id} requires attention: {operation.state}")


class OperationFailed(RuntimeError):
    def __init__(self, operation: Operation):
        self.operation = operation
        super().__init__(f"operation {operation.id} is {operation.state}")


class OperationsClient:
    def __init__(self, client: "ShifdeClient"):
        self.client = client

    async def get(self, operation_id: UUID | str) -> Operation:
        return Operation.model_validate(
            await self.client._request("GET", f"operations/{UUID(str(operation_id))}")
        )

    @overload
    async def wait_for_result(
        self,
        operation_id: UUID | str,
        *,
        expected_kind: Literal["build"],
        timeout: float = 60,
        poll_interval: float = 1,
    ) -> BuildArtifact: ...

    @overload
    async def wait_for_result(
        self,
        operation_id: UUID | str,
        *,
        expected_kind: Literal["plan"],
        timeout: float = 60,
        poll_interval: float = 1,
    ) -> DeploymentPlan: ...

    @overload
    async def wait_for_result(
        self,
        operation_id: UUID | str,
        *,
        expected_kind: Literal["deployment"],
        timeout: float = 60,
        poll_interval: float = 1,
    ) -> Deployment: ...

    async def wait_for_result(
        self,
        operation_id: UUID | str,
        *,
        expected_kind: Literal["build", "plan", "deployment"],
        timeout: float = 60,
        poll_interval: float = 1,
    ) -> BuildArtifact | DeploymentPlan | Deployment:
        if timeout <= 0 or poll_interval <= 0:
            raise ValueError("polling timeout and interval must be positive")
        async with asyncio.timeout(timeout):
            while True:
                op = await self.get(operation_id)
                if op.operation_kind != expected_kind:
                    raise ValueError("operation kind does not match requested result")
                if op.state in ("needs_attention", "waiting_for_policy"):
                    raise OperationAttentionRequired(op)
                if op.state in ("failed", "cancelled"):
                    raise OperationFailed(op)
                if op.state == "succeeded":
                    if op.result is None or op.result.result_kind != expected_kind:
                        raise ValueError("operation result kind is missing or invalid")
                    return op.result.value
                await asyncio.sleep(poll_interval)


class ResourceClient:
    def __init__(self, client: "ShifdeClient", resource: str, input_model: type[BaseModel]):
        self.client, self.resource, self.input_model = client, resource, input_model

    async def create(
        self, request: dict | BaseModel, *, idempotency_key: str | None = None
    ) -> Operation:
        body = self.input_model.model_validate(request).model_dump(mode="json")
        result = await self.client._request(
            "POST", self.resource, body=body, idempotency_key=idempotency_key or str(uuid4())
        )
        return Operation.model_validate(result)


class ShifdeClient:
    """HTTP facade for a deployed delivery service; it does not run that service."""

    def __init__(
        self,
        base_url: str,
        *,
        token_provider: TokenProvider,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        origin = Target._validate_host(base_url)
        self.token_provider = token_provider
        self.http = httpx.AsyncClient(
            base_url=origin + "/v1/",
            timeout=httpx.Timeout(30, connect=5),
            follow_redirects=False,
            transport=transport,
        )
        self.builds = ResourceClient(self, "builds", BuildSubmission)
        self.plans = ResourceClient(self, "plans", PlanSubmission)
        self.deployments = ResourceClient(self, "deployments", DeploymentSubmission)
        self.operations = OperationsClient(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.http.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        idempotency_key: str | None = None,
    ):
        pending = self.token_provider()
        token = await pending if inspect.isawaitable(pending) else pending
        if not isinstance(token, str) or not token or "\r" in token or "\n" in token:
            raise ValueError("token provider must return a nonempty bearer token")
        headers = {"Authorization": f"Bearer {token}"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        response = await self.http.request(method, path, json=body, headers=headers)
        if not 200 <= response.status_code < 300:
            code, trace_id = "REQUEST_FAILED", None
            try:
                error = response.json().get("error", {})
                if re.fullmatch(r"[A-Z0-9_]{1,80}", error.get("code", "")):
                    code = error["code"]
                trace_id = error.get("trace_id")
            except (ValueError, AttributeError, TypeError):
                pass
            raise ApiError(response.status_code, code, trace_id)
        return response.json()

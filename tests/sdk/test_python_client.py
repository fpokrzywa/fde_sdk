from uuid import uuid4

import httpx
import pytest
import respx


def operation(state="queued", **extra):
    return {
        "id": str(uuid4()),
        "tenant_id": str(uuid4()),
        "operation_kind": "build",
        "state": state,
        **extra,
    }


@respx.mock
async def test_client_sends_current_token_and_stable_idempotency_key():
    from shifde_sdk.client import ShifdeClient

    captured = []

    def respond(request):
        captured.append(
            (request.headers["Authorization"], request.headers["Idempotency-Key"], request.content)
        )
        return httpx.Response(202, json=operation())

    respx.post("https://delivery.example/v1/builds").mock(side_effect=respond)
    tokens = iter(["first", "refreshed"])
    async with ShifdeClient(
        "https://delivery.example", token_provider=lambda: next(tokens)
    ) as client:
        await client.builds.create(
            {"project_id": "11111111-1111-4111-8111-111111111111"}, idempotency_key="same-request"
        )
        await client.builds.create(
            {"project_id": "11111111-1111-4111-8111-111111111111"}, idempotency_key="same-request"
        )
    assert [item[:2] for item in captured] == [
        ("Bearer first", "same-request"),
        ("Bearer refreshed", "same-request"),
    ]


@respx.mock
async def test_wait_surfaces_attention_and_does_not_cancel_remote_operation():
    from shifde_sdk.client import OperationAttentionRequired, ShifdeClient

    value = operation("needs_attention")
    respx.get(f"https://delivery.example/v1/operations/{value['id']}").respond(200, json=value)
    async with ShifdeClient("https://delivery.example", token_provider=lambda: "token") as client:
        with pytest.raises(OperationAttentionRequired):
            await client.operations.wait_for_result(value["id"], expected_kind="build")
    assert all(call.request.method == "GET" for call in respx.calls)


@respx.mock
async def test_client_errors_do_not_expose_raw_server_body():
    from shifde_sdk.client import ApiError, ShifdeClient

    respx.post("https://delivery.example/v1/builds").respond(500, text="secret-token-do-not-expose")
    async with ShifdeClient("https://delivery.example", token_provider=lambda: "token") as client:
        with pytest.raises(ApiError) as error:
            await client.builds.create({"project_id": "11111111-1111-4111-8111-111111111111"})
    assert "secret-token" not in str(error.value)


@respx.mock
async def test_polling_timeout_does_not_submit_cancellation():
    from shifde_sdk.client import ShifdeClient

    value = operation()
    respx.get(f"https://delivery.example/v1/operations/{value['id']}").respond(200, json=value)
    async with ShifdeClient("https://delivery.example", token_provider=lambda: "token") as client:
        with pytest.raises(TimeoutError):
            await client.operations.wait_for_result(
                value["id"], expected_kind="build", timeout=0.01, poll_interval=0.005
            )
    assert all(call.request.method == "GET" for call in respx.calls)


def test_client_rejects_insecure_service_origin():
    from shifde_sdk.client import ShifdeClient

    with pytest.raises(ValueError):
        ShifdeClient("http://delivery.example", token_provider=lambda: "token")


@respx.mock
async def test_invalid_submission_fails_before_network_request():
    from pydantic import ValidationError
    from shifde_sdk.client import ShifdeClient

    async with ShifdeClient("https://delivery.example", token_provider=lambda: "token") as client:
        with pytest.raises(ValidationError):
            await client.builds.create({"project_id": "not-a-uuid"})
    assert len(respx.calls) == 0

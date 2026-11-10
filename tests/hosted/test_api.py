import time
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration


@pytest.fixture
def api_setup(hosted_service):
    from shifde_api.main import create_app
    from shifde_hosted.auth import JWTVerifier

    service, actor = hosted_service
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = JWTVerifier(
        issuer="https://issuer.test",
        audience="https://delivery.test",
        keys={"test": private.public_key()},
    )

    def token(subject="owner", tenant=None, audience="https://delivery.test", **extra):
        claims = {
            "sub": subject,
            "tenant_id": str(tenant or actor.tenant_id),
            "iss": "https://issuer.test",
            "aud": audience,
            "iat": int(time.time()),
            "exp": int(time.time()) + 600,
            "scope": " ".join(actor.scopes),
            **extra,
        }
        return jwt.encode(claims, private, algorithm="RS256", headers={"kid": "test"})

    app = create_app(service, verifier, public_url="https://delivery.test")
    with TestClient(app, base_url="https://delivery.test") as client:
        yield client, token, service, actor, private


def test_http_requires_verified_identity_and_database_role(api_setup):
    client, token, *_ = api_setup
    request = {"name": "test", "kind": "databricks.app/v1"}
    assert client.post("/v1/applications", json=request).status_code == 401
    response = client.post(
        "/v1/applications",
        json=request,
        headers={"Authorization": "Bearer " + token("reader", role="owner")},
    )
    assert response.status_code == 403
    assert (
        client.post(
            "/v1/applications",
            json=request,
            headers={"Authorization": "Bearer " + token(audience="https://wrong.test")},
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/v1/applications",
            json=request,
            headers={"Authorization": "Bearer " + token(exp=int(time.time()) - 1)},
        ).status_code
        == 401
    )


def test_http_submission_returns_durable_operation_location_and_events(api_setup):
    client, token, *_ = api_setup
    headers = {"Authorization": "Bearer " + token()}
    created = client.post(
        "/v1/applications", json={"name": "test", "kind": "databricks.app/v1"}, headers=headers
    )
    assert created.status_code == 201
    request = {"application_id": created.json()["id"], "spec": {"kind": "databricks.app/v1"}}
    response = client.post(
        "/v1/scaffolds", json=request, headers={**headers, "Idempotency-Key": "scaffold-1"}
    )
    assert response.status_code == 202
    op = response.json()
    assert response.headers["Location"] == "/v1/operations/" + op["id"]
    replay = client.post(
        "/v1/scaffolds", json=request, headers={**headers, "Idempotency-Key": "scaffold-1"}
    )
    assert replay.json()["id"] == op["id"]
    page = client.get(response.headers["Location"] + "/events", headers=headers).json()
    assert page["data"][0]["name"] == "submitted"
    assert (
        client.get(
            response.headers["Location"] + "/events?after=" + str(page["next_cursor"]),
            headers=headers,
        ).json()["data"]
        == []
    )


def test_cross_tenant_lookup_and_validation_do_not_echo_inputs(api_setup):
    client, token, *_ = api_setup
    headers = {"Authorization": "Bearer " + token()}
    response = client.post(
        "/v1/applications", json={"name": "test", "kind": "secret-native-token"}, headers=headers
    )
    assert response.status_code == 422 and "secret-native-token" not in response.text
    created = client.post(
        "/v1/applications", json={"name": "test", "kind": "databricks.app/v1"}, headers=headers
    ).json()
    other = {"Authorization": "Bearer " + token(tenant=uuid4())}
    assert client.get("/v1/applications/" + created["id"], headers=other).status_code == 403


def test_server_exposes_generated_openapi(api_setup):
    client, *_ = api_setup
    schema = client.get("/openapi.json").json()
    assert "/v1/builds" in schema["paths"]
    assert schema["paths"]["/v1/deployments"]["post"]["responses"]["202"]

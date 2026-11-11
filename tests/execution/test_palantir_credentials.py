from datetime import timedelta
from urllib.parse import parse_qs
from uuid import uuid4

import httpx
import pytest
from shifde_contracts.common import Target, utcnow
from shifde_contracts.jobs import CredentialGrant, ExecutionJob
from shifde_executor.config import ExecutorSettings, LocalCredentialBinding
from shifde_executor.credentials import LocalCredentialBroker


@pytest.mark.parametrize("expired_grant", [False, True])
def test_enrollment_oauth_exchange_is_bound_and_rechecks_expiry(
    tmp_path, monkeypatch, expired_grant
):
    from shifde_executor import credentials

    tenant, pool, worker = uuid4(), uuid4(), uuid4()
    target = Target(tenant_id=tenant, connection_id=uuid4(), host="https://enrollment.example.test")
    settings = ExecutorSettings(
        gateway_url="https://delivery.test",
        token_file=tmp_path / "identity",
        tenant_id=tenant,
        pool_id=pool,
        worker_id=worker,
        image="example/worker@sha256:" + "a" * 64,
        isolation="development",
        credential_bindings={
            "foundry": LocalCredentialBinding(
                tenant_id=tenant,
                host=target.host,
                platform="palantir",
                mode="palantir_client_credentials",
                client_id_env="TEST_FOUNDRY_ID",
                client_secret_env="TEST_FOUNDRY_SECRET",
                palantir_scopes=["api:ontologies-read"],
                allow_marketplace_preview=True,
            )
        },
    )
    now = utcnow()
    clock = [now]
    job = ExecutionJob(
        tenant_id=tenant,
        operation_id=uuid4(),
        execution_id=uuid4(),
        operation_fence=1,
        pool_id=pool,
        action="plan",
        image=settings.image,
        request={},
        target=target,
        toolchain_digest="a" * 64,
        deadline=now + timedelta(minutes=5),
        credential_ref="foundry",
        application_kind="palantir.marketplace/v1",
    )
    grant = CredentialGrant(
        tenant_id=tenant,
        job_id=job.id,
        worker_id=worker,
        pool_id=pool,
        target_id=target.id,
        credential_ref="foundry",
        audience=target.host,
        expires_at=now + timedelta(seconds=30),
    )
    monkeypatch.setenv("TEST_FOUNDRY_ID", "test-id")
    monkeypatch.setenv("TEST_FOUNDRY_SECRET", "local-client-secret")
    calls = []

    def token(request):
        calls.append(request)
        assert str(request.url) == target.host + "/multipass/api/oauth2/token"
        assert parse_qs(request.content.decode()) == {
            "grant_type": ["client_credentials"],
            "client_id": ["test-id"],
            "client_secret": ["local-client-secret"],
            "scope": ["api:ontologies-read"],
        }
        if expired_grant:
            clock[0] += timedelta(seconds=31)
        return httpx.Response(
            200,
            json={
                "access_token": "only-local-bearer",
                "token_type": "Bearer",
                "expires_in": "3600",
            },
        )

    original = httpx.Client

    def client(**kwargs):
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        return original(**kwargs, transport=httpx.MockTransport(token))

    monkeypatch.setattr(httpx, "Client", client)
    monkeypatch.setattr(credentials, "utcnow", lambda: clock[0])
    if expired_grant:
        with pytest.raises(PermissionError, match="expired"):
            LocalCredentialBroker(settings).resolve(grant, job)
    else:
        handle = LocalCredentialBroker(settings).resolve(grant, job)
        assert handle.bearer.get_secret_value() == "only-local-bearer"
        assert "only-local-bearer" not in repr(handle)
    assert len(calls) == 1
    assert (
        "local-client-secret"
        not in grant.model_dump_json() + job.model_dump_json() + settings.model_dump_json()
    )

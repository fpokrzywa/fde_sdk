from datetime import timedelta
from uuid import uuid4

import pytest
from shifde_contracts.common import Target, utcnow
from shifde_contracts.jobs import CredentialGrant, ExecutionJob
from shifde_executor.config import ExecutorSettings, LocalCredentialBinding
from shifde_executor.credentials import LocalCredentialBroker


def test_local_broker_resolves_bound_credential_without_serializing_secret(tmp_path, monkeypatch):
    tenant, pool, worker = uuid4(), uuid4(), uuid4()
    target = Target(tenant_id=tenant, connection_id=uuid4(), host="https://workspace.example")
    settings = ExecutorSettings(
        gateway_url="https://delivery.test",
        token_file=tmp_path / "identity",
        tenant_id=tenant,
        pool_id=pool,
        worker_id=worker,
        image="example/worker@sha256:" + "a" * 64,
        isolation="development",
        credential_bindings={
            "private": LocalCredentialBinding(
                tenant_id=tenant,
                host=target.host,
                mode="token_env",
                token_env="TEST_LOCAL_NATIVE_TOKEN",
            )
        },
    )
    job = ExecutionJob(
        tenant_id=tenant,
        operation_id=uuid4(),
        execution_id=uuid4(),
        operation_fence=1,
        pool_id=pool,
        action="apply",
        image=settings.image,
        request={},
        target=target,
        toolchain_digest="a" * 64,
        deadline=utcnow() + timedelta(minutes=5),
        credential_ref="private",
    )
    grant = CredentialGrant(
        tenant_id=tenant,
        job_id=job.id,
        worker_id=worker,
        pool_id=pool,
        target_id=target.id,
        credential_ref="private",
        audience=target.host,
        expires_at=utcnow() + timedelta(seconds=30),
    )
    monkeypatch.setenv("TEST_LOCAL_NATIVE_TOKEN", "secret-native-only-local")

    def forbidden_sdk(**kwargs):
        raise AssertionError("PAT resolution must not perform SDK discovery")

    monkeypatch.setattr("shifde_executor.credentials.WorkspaceClient", forbidden_sdk)
    handle = LocalCredentialBroker(settings).resolve(grant, job)
    assert handle.bearer.get_secret_value() == "secret-native-only-local"
    assert "secret-native-only-local" not in repr(handle)
    assert (
        "secret-native-only-local"
        not in grant.model_dump_json() + job.model_dump_json() + settings.model_dump_json()
    )
    for changes in (
        {"audience": "https://wrong.example"},
        {"expires_at": utcnow()},
        {"worker_id": uuid4()},
        {"job_id": uuid4()},
        {"tenant_id": uuid4()},
    ):
        with pytest.raises(PermissionError):
            LocalCredentialBroker(settings).resolve(grant.model_copy(update=changes), job)


def test_oauth_grant_is_rechecked_after_slow_token_resolution(tmp_path, monkeypatch):
    from shifde_executor import credentials

    tenant, pool, worker = uuid4(), uuid4(), uuid4()
    target = Target(tenant_id=tenant, connection_id=uuid4(), host="https://workspace.example")
    settings = ExecutorSettings(
        gateway_url="https://delivery.test",
        token_file=tmp_path / "identity",
        tenant_id=tenant,
        pool_id=pool,
        worker_id=worker,
        image="example/worker@sha256:" + "a" * 64,
        isolation="development",
        credential_bindings={
            "private": LocalCredentialBinding(
                tenant_id=tenant,
                host=target.host,
                mode="oauth_env",
                client_id_env="TEST_OAUTH_ID",
                client_secret_env="TEST_OAUTH_SECRET",
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
        action="apply",
        image=settings.image,
        request={},
        target=target,
        toolchain_digest="a" * 64,
        deadline=now + timedelta(minutes=5),
        credential_ref="private",
    )
    grant = CredentialGrant(
        tenant_id=tenant,
        job_id=job.id,
        worker_id=worker,
        pool_id=pool,
        target_id=target.id,
        credential_ref="private",
        audience=target.host,
        expires_at=now + timedelta(seconds=30),
    )

    class SlowSDK:
        config = None

        def __init__(self, **kwargs):
            self.config = self

        def authenticate(self):
            clock[0] += timedelta(seconds=31)
            return {"Authorization": "Bearer expired-grant-token"}

    monkeypatch.setenv("TEST_OAUTH_ID", "id")
    monkeypatch.setenv("TEST_OAUTH_SECRET", "secret")
    monkeypatch.setattr(credentials, "WorkspaceClient", SlowSDK)
    monkeypatch.setattr(credentials, "Config", lambda **kwargs: kwargs)
    monkeypatch.setattr(credentials, "utcnow", lambda: clock[0])
    with pytest.raises(PermissionError, match="expired"):
        LocalCredentialBroker(settings).resolve(grant, job)

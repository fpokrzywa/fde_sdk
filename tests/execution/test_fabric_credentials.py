from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from shifde_contracts.common import Target, utcnow
from shifde_contracts.jobs import CredentialGrant, ExecutionJob
from shifde_executor.config import ExecutorSettings, LocalCredentialBinding
from shifde_executor.credentials import LocalCredentialBroker


@pytest.mark.parametrize(
    "mode", ["azure_client_secret", "azure_managed_identity", "azure_workload_identity"]
)
def test_fabric_azure_identity_is_explicit_scoped_and_local(tmp_path, monkeypatch, mode):
    import azure.identity

    tenant, pool, worker, azure_tenant, client = [uuid4() for _ in range(5)]
    target = Target(
        tenant_id=tenant, connection_id=uuid4(), host="https://api.fabric.microsoft.com"
    )
    binding = LocalCredentialBinding(
        tenant_id=tenant,
        host=target.host,
        platform="fabric",
        mode=mode,
        azure_tenant_id=azure_tenant,
        azure_client_id=client,
        client_secret_env="TEST_FABRIC_ENTRA_SECRET",
        azure_federated_token_file=tmp_path / "federated-token",
    )
    settings = ExecutorSettings(
        gateway_url="https://delivery.test",
        token_file=tmp_path / "gateway-token",
        tenant_id=tenant,
        pool_id=pool,
        worker_id=worker,
        image="test/worker@sha256:" + "a" * 64,
        isolation="development",
        credential_bindings={"fabric": binding},
    )
    job = ExecutionJob(
        tenant_id=tenant,
        application_kind="fabric.solution/v1",
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
        credential_ref="fabric",
    )
    grant = CredentialGrant(
        tenant_id=tenant,
        job_id=job.id,
        worker_id=worker,
        pool_id=pool,
        target_id=target.id,
        credential_ref="fabric",
        audience=target.host,
        expires_at=utcnow() + timedelta(seconds=30),
    )
    calls = []

    class Identity:
        def __init__(self, *args, **kwargs):
            calls.append((args, kwargs))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            calls.append("closed")

        def get_token(self, scope):
            assert scope == "https://api.fabric.microsoft.com/.default"
            return SimpleNamespace(
                token="native-fabric-azure-access-token",
                expires_on=(utcnow() + timedelta(minutes=5)).timestamp(),
            )

    for name in [
        "ClientSecretCredential",
        "ManagedIdentityCredential",
        "WorkloadIdentityCredential",
    ]:
        monkeypatch.setattr(azure.identity, name, Identity)
    monkeypatch.setenv("TEST_FABRIC_ENTRA_SECRET", "local-client-secret")
    handle = LocalCredentialBroker(settings).resolve(grant, job)
    assert (
        handle.bearer.get_secret_value() == "native-fabric-azure-access-token"
        and calls[-1] == "closed"
    )
    assert (
        "native-fabric-azure-access-token"
        not in repr(handle)
        + grant.model_dump_json()
        + job.model_dump_json()
        + settings.model_dump_json()
    )
    with pytest.raises(PermissionError, match="IQ"):
        LocalCredentialBroker(settings).resolve(
            grant, job.model_copy(update={"application_kind": "fabric.iq/v1"})
        )
    for changes in [
        {"audience": "https://evil.example"},
        {"job_id": uuid4()},
        {"expires_at": utcnow()},
        {"tenant_id": uuid4()},
    ]:
        with pytest.raises(PermissionError):
            LocalCredentialBroker(settings).resolve(grant.model_copy(update=changes), job)

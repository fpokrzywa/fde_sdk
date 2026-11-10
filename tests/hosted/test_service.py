import hashlib
from uuid import uuid4

import pytest
import sqlalchemy as sa
from shifde_contracts.hosted import (
    ApplicationSubmission,
    ConnectionSubmission,
    ScaffoldSubmission,
    SourceUploadSubmission,
    TargetPatch,
    TargetSubmission,
)
from shifde_contracts.platform_specs.databricks import DatabricksApplicationSpec
from shifde_core.artifact_store import LocalArtifactStore, create_archive

pytestmark = pytest.mark.integration


@pytest.fixture
def hosted_service(database, new_tenant, hosted_database_url, tmp_path):
    from shifde_hosted.auth import Principal
    from shifde_hosted.config import CredentialBinding
    from shifde_hosted.persistence.models import memberships
    from shifde_hosted.service import ApplicationService

    tenant = new_tenant()
    engine = sa.create_engine(hosted_database_url[1])
    with engine.begin() as connection:
        connection.execute(
            memberships.insert().values(tenant_id=tenant, subject="owner", role="owner")
        )
        connection.execute(
            memberships.insert().values(tenant_id=tenant, subject="reader", role="reader")
        )
    engine.dispose()
    scopes = frozenset(
        "shifde:" + action for action in ["read", "write", "deploy", "admin", "approve"]
    )
    principal = Principal(tenant, "owner", scopes)
    binding = CredentialBinding(
        tenant_id=tenant,
        host=f"https://{tenant.hex}.example",
        mode="token_env",
        token_env="SHIFDE_TEST_NATIVE_TOKEN",
    )
    service = ApplicationService(
        database,
        LocalArtifactStore(tmp_path / "artifacts"),
        credential_bindings={"development": binding},
    )
    return service, principal


def test_operation_and_outbox_are_atomic_and_idempotent(hosted_service):
    from shifde_hosted.errors import ServiceError
    from shifde_hosted.persistence.models import operations, outbox

    service, actor = hosted_service
    app = service.create_application(
        actor, ApplicationSubmission(name="app", kind="databricks.app/v1")
    )
    request = ScaffoldSubmission(application_id=app.id, spec=DatabricksApplicationSpec())
    first = service.submit(actor, "scaffold", request, "stable-key")
    assert service.submit(actor, "scaffold", request, "stable-key").id == first.id
    other = service.create_application(
        actor, ApplicationSubmission(name="other", kind="databricks.app/v1")
    )
    with pytest.raises(ServiceError, match="IDEMPOTENCY_CONFLICT"):
        service.submit(
            actor, "scaffold", request.model_copy(update={"application_id": other.id}), "stable-key"
        )
    with service.database.transaction(actor.tenant_id) as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(operations)) == 1
        assert connection.scalar(sa.select(sa.func.count()).select_from(outbox)) == 1


def test_outbox_failure_rolls_back_operation(hosted_service):
    from shifde_hosted.persistence.models import operations

    service, actor = hosted_service
    app = service.create_application(
        actor, ApplicationSubmission(name="app", kind="databricks.app/v1")
    )

    def fail_outbox(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO outbox"):
            raise RuntimeError("injected outbox failure")

    sa.event.listen(service.database.engine, "before_cursor_execute", fail_outbox)
    try:
        with pytest.raises(RuntimeError):
            service.submit(
                actor,
                "scaffold",
                ScaffoldSubmission(application_id=app.id, spec=DatabricksApplicationSpec()),
                "key",
            )
    finally:
        sa.event.remove(service.database.engine, "before_cursor_execute", fail_outbox)
    with service.database.transaction(actor.tenant_id) as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(operations)) == 0


def test_reader_cannot_submit_builds_or_register_credentials(hosted_service):
    from shifde_hosted.auth import Principal
    from shifde_hosted.errors import Forbidden

    service, owner = hosted_service
    actor = Principal(owner.tenant_id, "reader", owner.scopes)
    with pytest.raises(Forbidden):
        service.create_connection(
            actor,
            ConnectionSubmission(
                name="dev", host="https://workspace.example", credential_ref="development"
            ),
        )
    with pytest.raises(Forbidden):
        service.submit(
            actor,
            "scaffold",
            ScaffoldSubmission(application_id=uuid4(), spec=DatabricksApplicationSpec()),
            "key",
        )


def test_source_upload_checks_declared_digest_and_tenant(hosted_service):
    from shifde_hosted.auth import Principal
    from shifde_hosted.errors import ServiceError

    service, actor = hosted_service
    data = create_archive({"file.txt": b"test"})
    grant = service.create_upload(
        actor, SourceUploadSubmission(digest=hashlib.sha256(data).hexdigest(), size=len(data))
    )
    with pytest.raises(ServiceError, match="SOURCE_MISMATCH"):
        service.upload_content(actor, grant.id, data + b"tampered")
    service.upload_content(actor, grant.id, data)
    source = service.complete_upload(actor, grant.id)
    assert source.digest == hashlib.sha256(data).hexdigest()
    assert service.complete_upload(actor, grant.id).id == source.id
    with pytest.raises(ServiceError):
        service.get_resource(Principal(uuid4(), "owner", actor.scopes), "source", source.id)


def test_credentials_are_bound_to_operator_approved_host_and_revision(hosted_service):
    from shifde_hosted.errors import ServiceError

    service, actor = hosted_service
    with pytest.raises(ServiceError):
        service.create_connection(
            actor,
            ConnectionSubmission(
                name="steal", host="https://attacker.example", credential_ref="development"
            ),
        )
    conn = service.create_connection(
        actor,
        ConnectionSubmission(
            name="dev",
            host=service.credential_bindings["development"].host,
            credential_ref="development",
        ),
    )
    target = service.create_target(
        actor, TargetSubmission(connection_id=conn.id, bindings={"warehouse": "123"})
    )
    updated, revision = service.patch_target(
        actor, target.id, TargetPatch(bindings={"warehouse": "456"}), expected_revision=1
    )
    assert updated.bindings["warehouse"] == "456" and revision == 2
    with pytest.raises(ServiceError, match="REVISION_CONFLICT"):
        service.patch_target(
            actor, target.id, TargetPatch(bindings={"warehouse": "789"}), expected_revision=1
        )

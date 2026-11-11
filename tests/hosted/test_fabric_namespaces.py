from uuid import uuid4

import pytest
import sqlalchemy as sa
from shifde_contracts.hosted import ConnectionSubmission, TargetPatch, TargetSubmission
from shifde_hosted.auth import Principal
from shifde_hosted.config import CredentialBinding
from shifde_hosted.errors import ServiceError
from shifde_hosted.persistence import models as m

pytestmark = pytest.mark.integration


def test_global_fabric_origin_is_shared_but_native_workspaces_are_tenant_owned(
    hosted_service, new_tenant, hosted_database_url
):
    service, first = hosted_service
    other = new_tenant()
    second = Principal(other, "owner", first.scopes)
    engine = sa.create_engine(hosted_database_url[1])
    with engine.begin() as c:
        c.execute(m.memberships.insert().values(tenant_id=other, subject="owner", role="owner"))
    engine.dispose()
    host = "https://api.fabric.microsoft.com"
    connections = []
    for actor in [first, second]:
        ref = "fabric-" + actor.tenant_id.hex
        service.credential_bindings[ref] = CredentialBinding(
            tenant_id=actor.tenant_id, host=host, mode="private"
        )
        connections.append(
            service.create_connection(
                actor, ConnectionSubmission(name="fabric", host=host, credential_ref=ref)
            )
        )
    workspace = str(uuid4())
    service.create_target(
        first, TargetSubmission(connection_id=connections[0].id, bindings={"workspace": workspace})
    )
    target = service.create_target(
        second,
        TargetSubmission(connection_id=connections[1].id, bindings={"workspace": str(uuid4())}),
    )
    with pytest.raises(ServiceError, match="NAMESPACE_CONFLICT"):
        service.create_target(
            second,
            TargetSubmission(connection_id=connections[1].id, bindings={"workspace": workspace}),
        )
    with pytest.raises(ServiceError, match="NAMESPACE_CONFLICT"):
        service.patch_target(
            second, target.id, TargetPatch(bindings={"workspace": workspace}), expected_revision=1
        )

from uuid import uuid4

import pytest
import sqlalchemy as sa

pytestmark = pytest.mark.integration


def test_rls_hides_other_tenants_and_resets_pooled_context(database, new_tenant):
    from shifde_hosted.persistence.models import resources

    tenant_a, tenant_b = new_tenant(), new_tenant()
    with database.transaction(tenant_a) as connection:
        connection.execute(
            resources.insert().values(
                tenant_id=tenant_a, id=uuid4(), kind="application", data={"name": "private"}
            )
        )
    with database.transaction(tenant_b) as connection:
        assert connection.execute(sa.select(resources)).all() == []
    with database.engine.connect() as connection:
        assert connection.execute(sa.select(resources)).all() == []


def test_cross_tenant_resource_link_is_rejected_by_database(database, new_tenant):
    from shifde_hosted.persistence.models import resource_links, resources

    tenant_a, tenant_b, parent, child = new_tenant(), new_tenant(), uuid4(), uuid4()
    with database.transaction(tenant_a) as connection:
        connection.execute(
            resources.insert().values(tenant_id=tenant_a, id=parent, kind="project", data={})
        )
    with database.transaction(tenant_b) as connection:
        connection.execute(
            resources.insert().values(tenant_id=tenant_b, id=child, kind="source", data={})
        )
    with pytest.raises(sa.exc.IntegrityError):
        with database.transaction(tenant_a) as connection:
            connection.execute(
                resource_links.insert().values(
                    tenant_id=tenant_a, parent_id=parent, relation="source", child_id=child
                )
            )


def test_runtime_role_cannot_update_audit_events(database):
    from shifde_hosted.persistence.models import events

    with pytest.raises(sa.exc.ProgrammingError):
        with database.transaction(uuid4()) as connection:
            connection.execute(events.update().values(name="tampered"))


def test_runtime_refuses_superuser_connection(hosted_database_url):
    from shifde_hosted.persistence.database import Database

    _, owner = hosted_database_url
    db = Database(owner)
    with pytest.raises(RuntimeError, match="role"):
        db.check_runtime()
    db.close()

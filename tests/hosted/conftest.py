import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url

from tests.hosted.test_executor import execution_setup as execution_setup
from tests.hosted.test_service import hosted_service as hosted_service


@pytest.fixture(scope="session")
def hosted_database_url():
    config = Path(".tools/hosted-dev.json")
    admin_url = os.getenv("SHIFDE_TEST_ADMIN_URL")
    if not admin_url and config.exists():
        admin_url = json.loads(config.read_bytes())["admin_url"]
    if not admin_url:
        pytest.skip("PostgreSQL integration requires SHIFDE_TEST_ADMIN_URL")
    from shifde_hosted.persistence.database import grant_runtime, migrate

    name = "shifde_test_" + uuid4().hex[:12]
    role = "shifde_test_" + uuid4().hex[:12]
    password = uuid4().hex
    admin = sa.create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.exec_driver_sql(
            f"CREATE ROLE \"{role}\" LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD '{password}'"
        )
        connection.exec_driver_sql(f'CREATE DATABASE "{name}"')
    owner_url = make_url(admin_url).set(database=name)
    migrate(owner_url.render_as_string(hide_password=False))
    grant_runtime(owner_url.render_as_string(hide_password=False), role)
    runtime_url = owner_url.set(username=role, password=password).render_as_string(
        hide_password=False
    )
    yield runtime_url, owner_url.render_as_string(hide_password=False)
    with admin.connect() as connection:
        connection.exec_driver_sql(f'DROP DATABASE "{name}" WITH (FORCE)')
        connection.exec_driver_sql(f'DROP ROLE "{role}"')
    admin.dispose()


@pytest.fixture
def database(hosted_database_url):
    from shifde_hosted.persistence.database import Database

    runtime, _ = hosted_database_url
    db = Database(runtime)
    yield db
    db.close()


@pytest.fixture
def new_tenant(hosted_database_url):
    from shifde_hosted.persistence.models import tenants

    _, owner_url = hosted_database_url

    def create():
        identity = uuid4()
        engine = sa.create_engine(owner_url)
        with engine.begin() as connection:
            connection.execute(tenants.insert().values(id=identity))
        engine.dispose()
        return identity

    return create

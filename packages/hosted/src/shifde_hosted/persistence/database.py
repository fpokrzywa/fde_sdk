from contextlib import contextmanager
from importlib.resources import files
from uuid import UUID

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from shifde_hosted.persistence.models import TENANT_TABLES


class Database:
    def __init__(self, url: str):
        if not url.startswith("postgresql+psycopg://"):
            raise ValueError("hosted persistence requires PostgreSQL with psycopg")
        self.engine = sa.create_engine(
            url,
            pool_size=5,
            max_overflow=5,
            pool_pre_ping=True,
            connect_args={"connect_timeout": 5},
        )

    @contextmanager
    def transaction(self, tenant_id: UUID):
        with self.engine.begin() as connection:
            connection.execute(
                sa.text("SELECT set_config('shifde.tenant_id', :tenant, true)"),
                {"tenant": str(tenant_id)},
            )
            yield connection

    def check_runtime(self):
        with self.engine.connect() as connection:
            privileged: bool = connection.execute(
                sa.text(
                    "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user"
                )
            ).scalar_one()
            if privileged:
                raise RuntimeError("runtime database role must not bypass RLS")
            version: str = connection.exec_driver_sql(
                "SELECT version_num FROM alembic_version"
            ).scalar_one()
            if version != "0002_executors":
                raise RuntimeError("database migration version is not supported")

    def close(self):
        self.engine.dispose()


def migrate(url: str):
    config = Config()
    config.set_main_option(
        "script_location", str(files("shifde_hosted.persistence").joinpath("migrations"))
    )
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    command.upgrade(config, "head")


def grant_runtime(url: str, role: str):
    if not role.replace("_", "").isalnum():
        raise ValueError("invalid database role")
    engine = sa.create_engine(url)
    with engine.begin() as connection:
        connection.exec_driver_sql(f'GRANT USAGE ON SCHEMA public TO "{role}"')
        connection.exec_driver_sql(f'GRANT SELECT ON tenants, alembic_version TO "{role}"')
        for table in TENANT_TABLES:
            permissions = (
                "SELECT, INSERT" if table == "events" else "SELECT, INSERT, UPDATE, DELETE"
            )
            connection.exec_driver_sql(f'GRANT {permissions} ON "{table}" TO "{role}"')
    engine.dispose()

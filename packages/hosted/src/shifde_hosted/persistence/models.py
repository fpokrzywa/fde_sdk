import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

metadata = sa.MetaData()


def table(name, *columns):
    return sa.Table(name, metadata, *columns)


def tenant():
    return sa.Column("tenant_id", UUID(as_uuid=True), primary_key=True)


def identity():
    return sa.Column("id", UUID(as_uuid=True), primary_key=True)


tenants = table("tenants", identity(), sa.Column("enabled", sa.Boolean, server_default=sa.true()))
memberships = table(
    "memberships",
    tenant(),
    sa.Column("subject", sa.Text, primary_key=True),
    sa.Column("role", sa.Text),
    sa.Column("active", sa.Boolean, server_default=sa.true()),
)
resources = table(
    "resources",
    tenant(),
    identity(),
    sa.Column("kind", sa.Text),
    sa.Column("data", JSONB),
    sa.Column("revision", sa.Integer, server_default="1"),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
)
resource_links = table(
    "resource_links",
    tenant(),
    sa.Column("parent_id", UUID(as_uuid=True), primary_key=True),
    sa.Column("relation", sa.Text, primary_key=True),
    sa.Column("child_id", UUID(as_uuid=True)),
)
operations = table(
    "operations",
    tenant(),
    identity(),
    sa.Column("kind", sa.Text),
    sa.Column("state", sa.Text),
    sa.Column("request", JSONB),
    sa.Column("requester", sa.Text),
    sa.Column("scopes", JSONB),
    sa.Column("result", JSONB),
    sa.Column("error", JSONB),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.Column("started_at", sa.DateTime(timezone=True)),
    sa.Column("deadline", sa.DateTime(timezone=True)),
    sa.Column("cancel_requested", sa.Boolean, server_default=sa.false()),
    sa.Column("event_seq", sa.BigInteger, server_default="0"),
    sa.Column("execution_id", UUID(as_uuid=True)),
    sa.Column("fence", sa.BigInteger, server_default="0"),
)
operation_links = table(
    "operation_links",
    tenant(),
    sa.Column("operation_id", UUID(as_uuid=True), primary_key=True),
    sa.Column("relation", sa.Text, primary_key=True),
    sa.Column("resource_id", UUID(as_uuid=True)),
)
idempotency_keys = table(
    "idempotency_keys",
    tenant(),
    sa.Column("route", sa.Text, primary_key=True),
    sa.Column("key", sa.Text, primary_key=True),
    sa.Column("body_digest", sa.Text),
    sa.Column("operation_id", UUID(as_uuid=True)),
    sa.Column("expires_at", sa.DateTime(timezone=True)),
)
outbox = table(
    "outbox",
    tenant(),
    identity(),
    sa.Column("operation_id", UUID(as_uuid=True)),
    sa.Column("kind", sa.Text, server_default="start"),
    sa.Column("state", sa.Text, server_default="pending"),
    sa.Column("attempts", sa.Integer, server_default="0"),
    sa.Column("lease_until", sa.DateTime(timezone=True)),
    sa.Column("lease_id", UUID(as_uuid=True)),
    sa.Column("last_error", sa.Text),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
)
events = table(
    "events",
    tenant(),
    sa.Column("operation_id", UUID(as_uuid=True), primary_key=True),
    sa.Column("seq", sa.BigInteger, primary_key=True),
    sa.Column("name", sa.Text),
    sa.Column("data", JSONB, server_default="{}"),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
)
step_journal = table(
    "step_journal",
    tenant(),
    sa.Column("operation_id", UUID(as_uuid=True), primary_key=True),
    sa.Column("key", sa.Text, primary_key=True),
    sa.Column("input_digest", sa.Text),
    sa.Column("state", sa.Text),
    sa.Column("receipt", JSONB),
)
application_claims = table(
    "application_claims",
    tenant(),
    sa.Column("application_id", UUID(as_uuid=True), primary_key=True),
    sa.Column("target_id", UUID(as_uuid=True), primary_key=True),
    sa.Column("operation_id", UUID(as_uuid=True)),
    sa.Column("execution_id", UUID(as_uuid=True)),
    sa.Column("fence", sa.BigInteger),
)
resource_bindings = table(
    "resource_bindings",
    tenant(),
    sa.Column("application_id", UUID(as_uuid=True), primary_key=True),
    sa.Column("target_id", UUID(as_uuid=True), primary_key=True),
    sa.Column("key", sa.Text, primary_key=True),
    sa.Column("native_id", sa.Text),
    sa.Column("resource_type", sa.Text),
    sa.Column("data", JSONB),
)
managed_namespaces = table(
    "managed_namespaces",
    sa.Column("host", sa.Text, primary_key=True),
    sa.Column("tenant_id", UUID(as_uuid=True)),
)

TENANT_TABLES = [t.name for t in metadata.tables.values() if t.name != "tenants"]

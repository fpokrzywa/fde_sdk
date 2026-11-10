from typing import cast
from uuid import UUID, uuid4

import sqlalchemy as sa
from pydantic import BaseModel
from shifde_contracts.operations import Operation, OperationState

from shifde_hosted.auth import Principal
from shifde_hosted.errors import Forbidden, NotFound
from shifde_hosted.persistence import models as m

ROLE_ACTIONS = {
    "reader": {"read"},
    "editor": {"read", "write"},
    "deployer": {"read", "write", "deploy"},
    "owner": {"read", "write", "deploy", "admin", "approve"},
}
TERMINAL = {"succeeded", "failed", "cancelled"}


def authorize(connection, principal: Principal, action: str):
    row = connection.execute(
        sa.select(m.memberships.c.role)
        .join(m.tenants, m.tenants.c.id == m.memberships.c.tenant_id)
        .where(
            m.memberships.c.tenant_id == principal.tenant_id,
            m.memberships.c.subject == principal.subject,
            m.memberships.c.active.is_(True),
            m.tenants.c.enabled.is_(True),
        )
        .with_for_update(read=True, of=m.memberships)
    ).first()
    if (
        not row
        or action not in ROLE_ACTIONS[row.role]
        or f"shifde:{action}" not in principal.scopes
    ):
        raise Forbidden()


def resource(connection, tenant_id: UUID, kind: str, identity: UUID, *, lock: bool = False):
    query = sa.select(m.resources).where(
        m.resources.c.tenant_id == tenant_id,
        m.resources.c.id == identity,
        m.resources.c.kind == kind,
    )
    if lock:
        query = query.with_for_update()
    row = connection.execute(query).mappings().first()
    if row is None:
        raise NotFound()
    return row


def put_resource(
    connection,
    tenant_id: UUID,
    kind: str,
    model: BaseModel,
    *,
    identity: UUID | None = None,
    links: dict[str, UUID] | None = None,
):
    identity = identity or UUID(str(model.model_dump()["id"]))
    if hasattr(model, "tenant_id") and model.tenant_id != tenant_id:
        raise Forbidden()
    connection.execute(
        m.resources.insert().values(
            tenant_id=tenant_id, id=identity, kind=kind, data=model.model_dump(mode="json")
        )
    )
    for relation, child in (links or {}).items():
        connection.execute(
            m.resource_links.insert().values(
                tenant_id=tenant_id, parent_id=identity, relation=relation, child_id=child
            )
        )
    return identity


def replace_resource(connection, tenant_id: UUID, identity: UUID, model: BaseModel):
    connection.execute(
        m.resources.update()
        .where(m.resources.c.tenant_id == tenant_id, m.resources.c.id == identity)
        .values(data=model.model_dump(mode="json"), revision=m.resources.c.revision + 1)
    )


def operation(connection, tenant_id: UUID, identity: UUID, *, lock: bool = False):
    query = sa.select(m.operations).where(
        m.operations.c.tenant_id == tenant_id, m.operations.c.id == identity
    )
    if lock:
        query = query.with_for_update()
    row = connection.execute(query).mappings().first()
    if row is None:
        raise NotFound()
    return row


def operation_model(row) -> Operation:
    return Operation(
        id=row["id"],
        tenant_id=row["tenant_id"],
        operation_kind=row["kind"],
        state=row["state"],
        created_at=row["created_at"],
        result=row["result"],
        error=row["error"],
    )


def event(connection, tenant_id: UUID, operation_id: UUID, name: str, data: dict | None = None):
    sequence = connection.execute(
        m.operations.update()
        .where(m.operations.c.tenant_id == tenant_id, m.operations.c.id == operation_id)
        .values(event_seq=m.operations.c.event_seq + 1)
        .returning(m.operations.c.event_seq)
    ).scalar_one()
    connection.execute(
        m.events.insert().values(
            tenant_id=tenant_id, operation_id=operation_id, seq=sequence, name=name, data=data or {}
        )
    )


def set_state(connection, row, state: str, *, result=None, error=None):
    current = operation_model(row)
    if state != current.state:
        current.transition(cast(OperationState, state))
    connection.execute(
        m.operations.update()
        .where(m.operations.c.tenant_id == row["tenant_id"], m.operations.c.id == row["id"])
        .values(
            state=state,
            result=result.model_dump(mode="json") if isinstance(result, BaseModel) else result,
            error=error.model_dump(mode="json") if isinstance(error, BaseModel) else error,
        )
    )
    event(connection, row["tenant_id"], row["id"], state)


def enqueue(connection, tenant_id: UUID, operation_id: UUID, kind: str = "start"):
    connection.execute(
        m.outbox.insert().values(
            tenant_id=tenant_id, id=uuid4(), operation_id=operation_id, kind=kind
        )
    )

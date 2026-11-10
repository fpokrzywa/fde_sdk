from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

import sqlalchemy as sa

from shifde_hosted.persistence import models as m
from shifde_hosted.persistence.database import Database


@dataclass(frozen=True)
class OutboxLease:
    tenant_id: UUID
    id: UUID
    operation_id: UUID
    kind: str
    lease_id: UUID


class OutboxRepository:
    def __init__(self, database: Database):
        self.database = database

    def tenants(self):
        with self.database.engine.connect() as c:
            return list(c.scalars(sa.select(m.tenants.c.id).where(m.tenants.c.enabled.is_(True))))

    def lease(self, tenant_id: UUID, *, limit: int = 20) -> list[OutboxLease]:
        leased = []
        with self.database.transaction(tenant_id) as c:
            rows = (
                c.execute(
                    sa.select(m.outbox)
                    .where(
                        m.outbox.c.state.in_(["pending", "leased"]),
                        sa.or_(
                            m.outbox.c.lease_until.is_(None),
                            m.outbox.c.lease_until <= sa.func.now(),
                        ),
                    )
                    .order_by(m.outbox.c.created_at)
                    .with_for_update(skip_locked=True)
                    .limit(limit)
                )
                .mappings()
                .all()
            )
            for row in rows:
                lease_id = uuid4()
                c.execute(
                    m.outbox.update()
                    .where(m.outbox.c.id == row["id"])
                    .values(
                        state="leased",
                        lease_id=lease_id,
                        lease_until=sa.func.now() + timedelta(seconds=30),
                        attempts=m.outbox.c.attempts + 1,
                    )
                )
                leased.append(
                    OutboxLease(tenant_id, row["id"], row["operation_id"], row["kind"], lease_id)
                )
        return leased

    def sent(self, lease: OutboxLease) -> bool:
        with self.database.transaction(lease.tenant_id) as c:
            result = c.execute(
                m.outbox.update()
                .where(
                    m.outbox.c.id == lease.id,
                    m.outbox.c.lease_id == lease.lease_id,
                    m.outbox.c.state == "leased",
                )
                .values(state="dispatched", last_error=None)
            )
            return result.rowcount == 1

    def failed(self, lease: OutboxLease):
        with self.database.transaction(lease.tenant_id) as c:
            c.execute(
                m.outbox.update()
                .where(
                    m.outbox.c.id == lease.id,
                    m.outbox.c.lease_id == lease.lease_id,
                    m.outbox.c.state == "leased",
                )
                .values(
                    state="pending",
                    last_error="TEMPORAL_UNAVAILABLE",
                    lease_until=sa.func.now() + timedelta(seconds=5),
                )
            )

from contextlib import nullcontext
from uuid import UUID

import sqlalchemy as sa
from shifde_contracts.common import utcnow
from shifde_core.hashing import digest
from shifde_core.journal import NeedsAttention

from shifde_hosted.auth import Principal
from shifde_hosted.errors import ServiceError
from shifde_hosted.persistence import models as m
from shifde_hosted.persistence import repository as r
from shifde_hosted.persistence.database import Database


class CancellationRequested(RuntimeError):
    pass


class PostgresJournal:
    def __init__(
        self,
        database: Database,
        tenant_id: UUID,
        operation_id: UUID,
        execution_id: UUID,
        fence: int,
        connection=None,
    ):
        self.database, self.tenant_id, self.operation_id = database, tenant_id, operation_id
        self.execution_id, self.fence = execution_id, fence
        self.connection = connection

    def _transaction(self):
        return (
            nullcontext(self.connection)
            if self.connection is not None
            else self.database.transaction(self.tenant_id)
        )

    def _owner(self, c, *, writing: bool):
        row = r.operation(c, self.tenant_id, self.operation_id, lock=True)
        if row["execution_id"] != self.execution_id or row["fence"] != self.fence:
            raise NeedsAttention("stale execution owner")
        if writing:
            if row["cancel_requested"]:
                raise CancellationRequested()
            if row["state"] != "running" or row["deadline"] <= utcnow():
                raise NeedsAttention("operation cannot perform further native writes")
            r.authorize(
                c, Principal(self.tenant_id, row["requester"], frozenset(row["scopes"])), "deploy"
            )
        return row

    def guard(self):
        with self._transaction() as c:
            self._owner(c, writing=True)

    def intent(self, key: str, inputs: dict) -> bool:
        with self._transaction() as c:
            row = self._owner(c, writing=True)
            plan = r.resource(c, self.tenant_id, "plan", UUID(row["request"]["plan_id"]))["data"]
            if key not in {step["key"] for step in plan["steps"]} or inputs != {
                "plan": plan["digest"]
            }:
                raise ServiceError("UNPLANNED_NATIVE_STEP")
            old = (
                c.execute(
                    sa.select(m.step_journal).where(
                        m.step_journal.c.operation_id == self.operation_id,
                        m.step_journal.c.key == key,
                    )
                )
                .mappings()
                .first()
            )
            if old:
                if old["input_digest"] != digest(inputs):
                    raise ServiceError("CHECKPOINT_CONFLICT")
                if old["state"] != "complete":
                    raise NeedsAttention("native outcome requires reconciliation")
                return False
            c.execute(
                m.step_journal.insert().values(
                    tenant_id=self.tenant_id,
                    operation_id=self.operation_id,
                    key=key,
                    input_digest=digest(inputs),
                    state="intent",
                )
            )
            r.event(c, self.tenant_id, self.operation_id, "native_intent", {"step": key})
            return True

    def complete(self, key: str, receipt: dict):
        with self._transaction() as c:
            self._owner(c, writing=False)
            old = (
                c.execute(
                    sa.select(m.step_journal)
                    .where(
                        m.step_journal.c.operation_id == self.operation_id,
                        m.step_journal.c.key == key,
                    )
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            if not old or (old["state"] == "complete" and old["receipt"] != receipt):
                raise ServiceError("CHECKPOINT_CONFLICT")
            if old["state"] == "complete":
                return
            c.execute(
                m.step_journal.update()
                .where(
                    m.step_journal.c.operation_id == self.operation_id, m.step_journal.c.key == key
                )
                .values(state="complete", receipt=receipt)
            )
            r.event(c, self.tenant_id, self.operation_id, "native_receipt", {"step": key})

    def completed(self, key: str) -> dict | None:
        with self._transaction() as c:
            row = c.execute(
                sa.select(m.step_journal.c.receipt).where(
                    m.step_journal.c.operation_id == self.operation_id,
                    m.step_journal.c.key == key,
                    m.step_journal.c.state == "complete",
                )
            ).first()
            return row.receipt if row else None

    def has_intent(self, key: str) -> bool:
        with self._transaction() as c:
            return (
                c.execute(
                    sa.select(m.step_journal.c.key).where(
                        m.step_journal.c.operation_id == self.operation_id,
                        m.step_journal.c.key == key,
                    )
                ).first()
                is not None
            )

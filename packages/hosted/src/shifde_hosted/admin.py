"""Operator-only database setup. Never run with the hosted runtime identity."""

import argparse
import os
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from shifde_hosted.persistence import models as m
from shifde_hosted.persistence.database import grant_runtime, migrate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate")
    grant = commands.add_parser("grant-runtime")
    grant.add_argument("role")
    tenant = commands.add_parser("create-tenant")
    tenant.add_argument("--id", type=UUID, default=None)
    member = commands.add_parser("set-member")
    member.add_argument("tenant_id", type=UUID)
    member.add_argument("subject")
    member.add_argument("role", choices=["reader", "editor", "deployer", "owner"])
    args = parser.parse_args()
    url = os.environ["SHIFDE_ADMIN_DATABASE_URL"]
    if args.command == "migrate":
        migrate(url)
        return
    if args.command == "grant-runtime":
        grant_runtime(url, args.role)
        return
    engine = sa.create_engine(url)
    try:
        with engine.begin() as c:
            if args.command == "create-tenant":
                identity = args.id or uuid4()
                c.execute(m.tenants.insert().values(id=identity))
                print(identity)
            else:
                # SET LOCAL also supports an owner role subject to FORCE RLS.
                c.execute(
                    sa.text("SELECT set_config('shifde.tenant_id', :tenant, true)"),
                    {"tenant": str(args.tenant_id)},
                )
                c.execute(
                    insert(m.memberships)
                    .values(tenant_id=args.tenant_id, subject=args.subject, role=args.role)
                    .on_conflict_do_update(
                        index_elements=["tenant_id", "subject"],
                        set_={"role": args.role, "active": True},
                    )
                )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()

from importlib.resources import files

from alembic import op

revision = "0002_executors"
down_revision = "0001_hosted"
branch_labels = None
depends_on = None


def upgrade():
    op.get_bind().exec_driver_sql(
        files("shifde_hosted.persistence").joinpath("schema_v2.sql").read_text(),
        execution_options={"no_parameters": True},
    )


def downgrade():
    raise RuntimeError("executor receipts require an explicit restore plan")

from importlib.resources import files

from alembic import op

revision = "0001_hosted"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.get_bind().exec_driver_sql(
        files("shifde_hosted.persistence").joinpath("schema_v1.sql").read_text(),
        execution_options={"no_parameters": True},
    )


def downgrade():
    raise RuntimeError(
        "removing durable operation records requires an explicit database restore plan"
    )

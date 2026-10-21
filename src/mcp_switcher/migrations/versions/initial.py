"""Initial project and operation tables."""
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    db = op.get_context().config.attributes["database"]
    db.metadata.create_all(op.get_bind())


def downgrade():
    db = op.get_context().config.attributes["database"]
    db.metadata.drop_all(op.get_bind())

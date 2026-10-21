"""Keep epoch timestamps precise on MySQL and MariaDB."""
from alembic import op
from sqlalchemy import Float

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name in {"mysql", "mariadb"}:
        db = op.get_context().config.attributes["database"]
        op.alter_column(db.prefix + "_operations", "expires", existing_type=Float(),
                        type_=Float(53), existing_nullable=False)


def downgrade():
    # Wider precision remains compatible with the old schema; do not discard it.
    pass

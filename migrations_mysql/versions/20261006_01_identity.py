"""Identity/profile branch, separate from the frozen SQLite history."""
from alembic import op
from sqlalchemy import inspect
from migrations_mysql.identity_baseline_20261006 import build_metadata
metadata = build_metadata()
from app.core.mysql_schema_checks import validate_table
revision = "20261006_mysql_01"
down_revision = None
branch_labels = ("mysql_business",)
depends_on = None

def upgrade():
    conn = op.get_bind()
    if conn.dialect.name != "mysql":
        raise RuntimeError("MySQL branch only")
    for table in metadata.sorted_tables:
        if inspect(conn).has_table(table.name):
            validate_table(conn, table)
        else:
            # Explicit per-table DDL in a versioned revision. MySQL commits it;
            # interrupted runs validate earlier objects before resuming.
            table.create(conn)
            validate_table(conn, table)

def downgrade():
    raise RuntimeError("Restore an offline backup; MySQL DDL is not an atomic rollback")
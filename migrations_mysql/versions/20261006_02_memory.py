"""Ticket04 frozen conversation ownership, memory snapshots, and jobs."""
from alembic import op
from sqlalchemy import inspect
from migrations_mysql.memory_baseline_20261006 import build_memory_metadata
from app.core.mysql_schema_checks import validate_table

metadata = build_memory_metadata()
revision = "20261006_mysql_02"
down_revision = "20261006_mysql_01"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    if conn.dialect.name != "mysql":
        raise RuntimeError("MySQL branch only")
    for name in ("conversations", "conversation_memory_states", "memory_extraction_jobs"):
        table = metadata.tables[name]
        if inspect(conn).has_table(name):
            validate_table(conn, table)
        else:
            table.create(conn)
            validate_table(conn, table)


def downgrade():
    raise RuntimeError("Restore an offline backup; MySQL DDL is not an atomic rollback")

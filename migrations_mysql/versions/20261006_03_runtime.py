"""Atomic Runtime messages, planning briefs, Runs, interactions, and events."""
from alembic import op
from sqlalchemy import inspect

from app.core.mysql_schema_checks import validate_table
from migrations_mysql.runtime_baseline_20261006 import build_runtime_metadata

revision = "20261006_mysql_03"
down_revision = "20261006_mysql_02"
branch_labels = None
depends_on = None

_TABLES = (
    "messages", "runs", "planning_briefs", "active_brief_slots",
    "run_interaction_responses", "run_events",
)


def upgrade():
    conn = op.get_bind()
    if conn.dialect.name != "mysql":
        raise RuntimeError("MySQL branch only")
    metadata = build_runtime_metadata()
    for name in _TABLES:
        table = metadata.tables[name]
        if inspect(conn).has_table(name):
            validate_table(conn, table)
        else:
            table.create(conn)
            validate_table(conn, table)


def downgrade():
    raise RuntimeError("Restore an offline backup; MySQL DDL is not an atomic rollback")

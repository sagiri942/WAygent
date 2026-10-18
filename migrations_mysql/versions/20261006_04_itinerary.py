"""Itinerary enrichment and optimizer shadow results."""

from alembic import op
from sqlalchemy import inspect

from app.core.mysql_schema_checks import validate_table
from migrations_mysql.itinerary_baseline_20261006 import build_itinerary_metadata

revision = "20261006_mysql_04"
down_revision = "20261006_mysql_03"
branch_labels = None
depends_on = None

_TABLES = ("itinerary_enrichments", "optimizer_shadow_runs")


def upgrade():
    conn = op.get_bind()
    if conn.dialect.name != "mysql":
        raise RuntimeError("MySQL branch only")
    metadata = build_itinerary_metadata()
    for name in _TABLES:
        table = metadata.tables[name]
        if inspect(conn).has_table(name):
            validate_table(conn, table)
        else:
            table.create(conn)
            validate_table(conn, table)


def downgrade():
    raise RuntimeError("Restore an offline backup; MySQL DDL is not an atomic rollback")

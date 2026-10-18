"""Ticket07 import audit ledger and legacy profile storage."""

from alembic import op
from sqlalchemy import inspect

from app.core.mysql_schema_checks import validate_table
from migrations_mysql.business_import_baseline_20261006 import build_business_import_metadata

revision = "20261006_mysql_05"
down_revision = "20261006_mysql_04"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    if conn.dialect.name != "mysql":
        raise RuntimeError("MySQL branch only")
    metadata = build_business_import_metadata()
    for name in ("user_profiles", "business_import_ledger"):
        table = metadata.tables[name]
        if inspect(conn).has_table(name):
            validate_table(conn, table)
        else:
            table.create(conn)
            validate_table(conn, table)


def downgrade():
    raise RuntimeError("Restore an offline backup; MySQL DDL is not an atomic rollback")

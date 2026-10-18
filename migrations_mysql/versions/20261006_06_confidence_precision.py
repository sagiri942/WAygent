"""Widen confidence to DOUBLE; MySQL DDL recovery accepts only exact contracts."""
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects.mysql import DOUBLE, FLOAT
from app.core.mysql_schema_checks import validate_existing
from migrations_mysql.business_import_baseline_20261006 import build_business_import_metadata
from migrations_mysql.precision_baseline_20261006 import build_precision_metadata

revision = "20261006_mysql_06"
down_revision = "20261006_mysql_05"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    if connection.dialect.name != "mysql":
        raise RuntimeError("MySQL branch only")
    old = build_business_import_metadata()
    widened = build_precision_metadata()
    inspector = inspect(connection)
    if set(inspector.get_table_names()) != set(old.tables) | {"alembic_version"}:
        raise RuntimeError("MySQL schema differs: precision migration requires the complete revision 05")
    confidence_type = next((c["type"] for c in inspector.get_columns("memory_facts") if c["name"] == "confidence"), None)
    if isinstance(confidence_type, DOUBLE):
        # ALTER implicitly committed, but the process died before version update.
        validate_existing(connection, widened)
        return
    validate_existing(connection, old)
    op.alter_column("memory_facts", "confidence", type_=DOUBLE(), existing_type=FLOAT(),
                    existing_nullable=False, existing_server_default="1")
    validate_existing(connection, widened)


def downgrade():
    raise RuntimeError("Restore an offline backup; narrowing confidence would lose precision")

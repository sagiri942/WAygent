"""Explicit Alembic entry; no schema mutation during normal request handling."""
from pathlib import Path
from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext

_MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"

def upgrade_schema(database, revision="head"):
    if database.engine.dialect.name != "sqlite":
        # MySQL DDL implicitly commits: reject BEFORE Alembic can create its
        # version table. Later business migration tickets supply MySQL revisions.
        raise RuntimeError("Current business schema baseline is SQLite only")
    config = Config()
    config.set_main_option("script_location", str(_MIGRATIONS))
    with database.transaction() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, revision)


def schema_revision(database):
    with database.transaction() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def upgrade_mysql_schema(database, revision="head"):
    """Explicit staged MySQL expansion, never SQLite adoption or auto-cutover."""
    import hashlib
    from sqlalchemy import text, inspect
    from migrations_mysql.identity_baseline_20261006 import build_metadata as build_identity_metadata
    from migrations_mysql.memory_baseline_20261006 import build_memory_metadata
    from migrations_mysql.runtime_baseline_20261006 import build_runtime_metadata
    from migrations_mysql.itinerary_baseline_20261006 import build_itinerary_metadata
    from migrations_mysql.business_import_baseline_20261006 import build_business_import_metadata
    from migrations_mysql.precision_baseline_20261006 import build_precision_metadata
    from sqlalchemy.dialects.mysql import DOUBLE
    from app.core.mysql_schema_checks import validate_existing
    if database.engine.dialect.name != "mysql":
        raise RuntimeError("MySQL expansion requires MySQL")
    config = Config()
    config.set_main_option("script_location", str(_MIGRATIONS.parent / "migrations_mysql"))
    lock = "waygent-schema-" + hashlib.sha256(str(database.engine.url.database).encode()).hexdigest()[:32]
    with database.connection() as connection:
        acquired = connection.execute(text("SELECT GET_LOCK(:name, 30)"), {"name": lock}).scalar_one()
        if acquired != 1:
            raise RuntimeError("MySQL schema lock unavailable")
        try:
            current = MigrationContext.configure(connection).get_current_revision()
            if current not in {None, "20261006_mysql_01", "20261006_mysql_02", "20261006_mysql_03", "20261006_mysql_04", "20261006_mysql_05", "20261006_mysql_06"}:
                raise RuntimeError("Unknown MySQL schema revision; migrate explicitly")
            actual_tables = set(inspect(connection).get_table_names()) - {"alembic_version"}
            expected = build_business_import_metadata()
            if current == "20261006_mysql_06":
                expected = build_precision_metadata()
            elif current == "20261006_mysql_05" and "memory_facts" in actual_tables:
                confidence_type = next((c["type"] for c in inspect(connection).get_columns("memory_facts") if c["name"] == "confidence"), None)
                if isinstance(confidence_type, DOUBLE):
                    expected = build_precision_metadata()
            validate_existing(connection, expected)
            identity_tables = set(build_identity_metadata().tables)
            memory_tables = set(build_memory_metadata().tables)
            runtime_tables = set(build_runtime_metadata().tables)
            itinerary_tables = set(build_itinerary_metadata().tables)
            import_tables = set(build_business_import_metadata().tables)
            if current == "20261006_mysql_01" and (
                not identity_tables <= actual_tables or not actual_tables <= memory_tables
            ):
                raise RuntimeError("MySQL schema differs: identity or partial memory table missing")
            if current == "20261006_mysql_02" and (
                not memory_tables <= actual_tables or not actual_tables <= runtime_tables
            ):
                raise RuntimeError("MySQL schema differs: memory table missing or untracked table present")
            if current == "20261006_mysql_03" and (
                not runtime_tables <= actual_tables or not actual_tables <= itinerary_tables
            ):
                raise RuntimeError("MySQL schema differs: Runtime table missing or untracked itinerary object present")
            if current == "20261006_mysql_04" and (
                not itinerary_tables <= actual_tables or not actual_tables <= import_tables
            ):
                raise RuntimeError("MySQL schema differs: invalid partial business-import revision objects")
            if current in {"20261006_mysql_05", "20261006_mysql_06"} and actual_tables != import_tables:
                raise RuntimeError("MySQL schema differs: business-import table missing or untracked table present")
            connection.commit()
            config.attributes["connection"] = connection
            command.upgrade(config, revision)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": lock})
            connection.commit()

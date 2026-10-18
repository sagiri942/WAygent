"""Fail-closed application storage selection for the production lifespan."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping
import json
import os

from sqlalchemy import select
from sqlalchemy.engine import make_url

from app.core.core_storage import CoreDatabase


@dataclass(frozen=True)
class ProductionStorageConfig:
    business_dsn: str
    checkpoint_dsn: str


@dataclass
class ProductionStorage:
    business_database: CoreDatabase
    checkpoint_context_factory: object
    checkpoint_dsn: str

    def close(self) -> None:
        self.business_database.close()


def verify_business_storage_ready(database: CoreDatabase) -> None:
    """Require exactly one structurally complete, verified history import."""
    from app.core.business_history_import_schema import IMPORT_TABLES, import_ledger

    with database.transaction() as connection:
        rows = connection.execute(select(import_ledger)).mappings().all()
    if len(rows) != 1:
        raise RuntimeError("Business MySQL schema requires one completed business-history import")

    row = rows[0]
    try:
        manifest = json.loads(row["manifest_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Business MySQL history import ledger is malformed") from exc
    if not isinstance(manifest, dict):
        raise RuntimeError("Business MySQL history import ledger is malformed")

    source_id = row["source_id"]
    source_digest = row["source_digest"]
    table_counts = manifest.get("table_counts")
    table_hashes = manifest.get("table_hashes")
    omitted_counts = manifest.get("omitted_table_counts")
    omitted_presence = manifest.get("omitted_table_presence")
    expected_tables = set(IMPORT_TABLES)
    if (
        manifest.get("source_id") != source_id
        or manifest.get("source_digest") != source_digest
        or not isinstance(source_id, str)
        or not isinstance(source_digest, str)
        or len(source_id) != 64
        or len(source_digest) != 64
        or any(character not in "0123456789abcdef" for character in source_id + source_digest)
        or not isinstance(table_counts, dict)
        or set(table_counts) != expected_tables
        or not isinstance(table_hashes, dict)
        or set(table_hashes) != expected_tables
        or not isinstance(omitted_counts, dict)
        or set(omitted_counts) != {"pending_modifications"}
        or not isinstance(omitted_presence, dict)
        or set(omitted_presence) != {"pending_modifications"}
        or type(omitted_counts["pending_modifications"]) is not int
        or omitted_counts["pending_modifications"] < 0
        or type(omitted_presence["pending_modifications"]) is not bool
        or type(manifest.get("already_applied")) is not bool
        or any(type(count) is not int or count < 0 for count in table_counts.values())
        or any(
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            for digest in table_hashes.values()
        )
    ):
        raise RuntimeError("Business MySQL history import ledger is incomplete or inconsistent")


def resolve_production_storage_config(
    environment: Mapping[str, str] | None = None,
) -> ProductionStorageConfig:
    """Require separate, explicit MySQL schemas for business and checkpoints."""
    values = os.environ if environment is None else environment
    business = str(values.get("WAYGENT_MYSQL_DSN", "")).strip()
    checkpoint = str(values.get("WAYGENT_CHECKPOINT_DSN", "")).strip()
    if not business or not checkpoint:
        raise RuntimeError(
            "Production storage requires WAYGENT_MYSQL_DSN and "
            "WAYGENT_CHECKPOINT_DSN; SQLite fallback is disabled"
        )
    try:
        business_url, checkpoint_url = make_url(business), make_url(checkpoint)
    except Exception as exc:
        raise RuntimeError("Production storage DSNs are invalid") from exc
    if not business_url.get_backend_name().startswith("mysql") or not checkpoint_url.get_backend_name().startswith("mysql"):
        raise RuntimeError("Production business and checkpoint storage must use MySQL")
    if not business_url.database or not checkpoint_url.database:
        raise RuntimeError("Production storage DSNs must include explicit MySQL schema names")
    if business_url.database == checkpoint_url.database:
        raise RuntimeError("Business and checkpoint storage require separate MySQL schemas")
    return ProductionStorageConfig(business, checkpoint)


def create_production_storage(
    environment: Mapping[str, str] | None = None,
) -> ProductionStorage:
    config = resolve_production_storage_config(environment)
    database = CoreDatabase(config.business_dsn)
    try:
        from app.core.schema_management import schema_revision
        from app.core.mysql_schema_checks import validate_existing
        from migrations_mysql.precision_baseline_20261006 import build_precision_metadata

        if schema_revision(database) != "20261006_mysql_06":
            raise RuntimeError("Business MySQL schema must be fully imported at revision 06")
        with database.connection() as connection:
            expected_tables = set(build_precision_metadata().tables) | {"alembic_version"}
            from sqlalchemy import inspect
            if set(inspect(connection).get_table_names()) != expected_tables:
                raise RuntimeError("Business MySQL schema has missing or untracked tables")
            validate_existing(connection, build_precision_metadata())
        verify_business_storage_ready(database)

        from app.runtime.checkpoint_mysql import GuardedAIOMySQLSaver

        factory = lambda: GuardedAIOMySQLSaver.from_conn_string(config.checkpoint_dsn)
        return ProductionStorage(database, factory, config.checkpoint_dsn)
    except BaseException:
        database.close()
        raise


def bind_production_backends(storage: ProductionStorage) -> None:
    """Bind every API and Runtime repository to the verified business engine."""
    from app.api.auth_routes import configure_auth_backend
    from app.api.history_routes import configure_history_runtime
    from app.api.profile_routes import configure_profile_backend
    from app.api.runtime_routes import configure_runtime_backend
    from app.runtime.container import configure_runtime_storage

    database = storage.business_database
    configure_auth_backend(database)
    configure_profile_backend(database)
    manager, scheduler, chat_service = configure_runtime_backend(database)
    configure_history_runtime(manager_backend=manager, scheduler_backend=scheduler)
    configure_runtime_storage(
        database,
        storage.checkpoint_context_factory,
        manager_backend=manager,
        scheduler_backend=scheduler,
        chat_service_backend=chat_service,
    )


async def verify_checkpoint_storage(storage: ProductionStorage) -> None:
    """Require the frozen saver schema and a completed history-import ledger."""
    from app.runtime.checkpoint_history_migration import CheckpointHistoryMigration

    await CheckpointHistoryMigration(storage.checkpoint_dsn).verify_runtime_ready()

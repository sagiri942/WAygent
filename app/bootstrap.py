"""Initialize a new installation; never import or overwrite user history."""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import func, select, text

from app.core.core_storage import CoreDatabase
from app.core.schema_management import upgrade_mysql_schema
from app.core.business_history_import_schema import IMPORT_TABLES, import_ledger
from migrations_mysql.precision_baseline_20261006 import build_precision_metadata
from app.storage_runtime import resolve_production_storage_config, verify_business_storage_ready


def initialize_business(dsn: str) -> None:
    """Record an explicitly empty installation under the existing schema gate."""
    with CoreDatabase(dsn) as database:
        upgrade_mysql_schema(database)
        metadata = build_precision_metadata()
        lock = "waygent-install-" + hashlib.sha256(database.engine.url.database.encode()).hexdigest()[:32]
        with database.connection() as connection:
            if connection.execute(text("SELECT GET_LOCK(:name, 30)"), {"name": lock}).scalar_one() != 1:
                raise RuntimeError("Fresh installation lock is unavailable")
            connection.commit()
            try:
                with connection.begin():
                    initialized = connection.execute(select(import_ledger.c.source_id)).first()
                    if not initialized:
                        for name, table in metadata.tables.items():
                            if name != "business_import_ledger" and connection.execute(select(func.count()).select_from(table)).scalar_one():
                                raise RuntimeError("Refusing to initialize a nonempty business database without an installation ledger")
                        counts = {name: 0 for name in IMPORT_TABLES}
                        manifest = {
                            "installation": "fresh-empty-database-v1",
                            "source_id": hashlib.sha256(b"waygent:fresh-install:v1").hexdigest(),
                            "source_digest": hashlib.sha256(json.dumps(counts, sort_keys=True).encode()).hexdigest(),
                            "table_counts": counts,
                            "table_hashes": {name: hashlib.sha256(f"{name}:[]".encode()).hexdigest() for name in IMPORT_TABLES},
                            "omitted_table_counts": {"pending_modifications": 0},
                            "omitted_table_presence": {"pending_modifications": False},
                            "already_applied": False,
                        }
                        connection.execute(import_ledger.insert().values(
                            source_id=manifest["source_id"], source_digest=manifest["source_digest"],
                            manifest_json=json.dumps(manifest, sort_keys=True),
                            applied_at=datetime.now(timezone.utc).isoformat(),
                        ))
            finally:
                connection.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": lock})
                connection.commit()
        verify_business_storage_ready(database)


async def initialize_checkpoints(dsn: str) -> None:
    """Set up the validated saver and record zero imported checkpoints."""
    from app.runtime.checkpoint_history_migration import CheckpointHistoryMigration, _LEDGER_TABLE, _LEDGER_STATES
    from app.runtime.checkpoint_mysql import GuardedAIOMySQLSaver

    migration = CheckpointHistoryMigration(dsn)
    async with migration._locked_connection() as connection:
        async with GuardedAIOMySQLSaver.from_conn_string(dsn) as saver:
            await migration._prepare_saver(saver, connection)
            async with connection.cursor() as cursor:
                await cursor.execute(f"SELECT COUNT(*) FROM {_LEDGER_TABLE} WHERE status=%s", (_LEDGER_STATES["complete"],))
                initialized = (await cursor.fetchone())[0]
                if not initialized:
                    await cursor.execute(f"SELECT COUNT(*) FROM {_LEDGER_TABLE}")
                    if (await cursor.fetchone())[0] or any(await migration._target_counts(connection)):
                        raise RuntimeError("Refusing to initialize nonempty or partially imported checkpoint history")
                    digest = hashlib.sha256(b"waygent:fresh-empty-checkpoints:v1").hexdigest()
                    await cursor.execute(
                        f"INSERT INTO {_LEDGER_TABLE} (source_digest,status,checkpoint_count,write_count) VALUES (%s,%s,0,0)",
                        (digest, _LEDGER_STATES["complete"]),
                    )
            await connection.commit()
    await migration.verify_runtime_ready()


def initialize() -> None:
    config = resolve_production_storage_config()
    initialize_business(config.business_dsn)
    asyncio.run(initialize_checkpoints(config.checkpoint_dsn))
    print("WAygent storage is initialized and verified.", flush=True)


if __name__ == "__main__":
    initialize()

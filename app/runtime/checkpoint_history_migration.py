"""Fail-closed migration of a complete SQLite LangGraph history to MySQL.

The importer is intentionally pinned to the SQLite 3.1.1 / MySQL 3.0.0
formats validated by Ticket01. Checkpoints are read and written through each
saver's public tuple API. SQLite pending-write identity (including its index
and serialized value bytes) is not exposed by CheckpointTuple, so those rows
are transferred through an explicitly schema-checked adapter.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from contextlib import asynccontextmanager, closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


_SQLITE_TABLES = {
    "checkpoints": ("thread_id", "checkpoint_ns", "checkpoint_id", "parent_checkpoint_id", "type", "checkpoint", "metadata"),
    "writes": ("thread_id", "checkpoint_ns", "checkpoint_id", "task_id", "idx", "channel", "type", "value"),
}
_ROOT = Path(__file__).resolve().parents[2]
_EXPECTED_PACKAGES = {
    name: expected
    for line in (_ROOT / "requirements.checkpoint-gate.constraints.txt").read_text(encoding="utf-8").splitlines()
    if "==" in line and not line.lstrip().startswith("#")
    for name, expected in (line.split("==", 1),)
}
_LEDGER_TABLE = "checkpoint_history_imports"
_SAVER_TABLES = {"checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations"}
_TABLE_LAYOUTS = {
    "checkpoints": (
        ("thread_id", "varchar(150)", "NO", "utf8mb4", "utf8mb4_0900_bin", ""),
        ("checkpoint_ns", "varchar(2000)", "NO", "utf8mb4", "utf8mb4_0900_bin", ""),
        ("checkpoint_id", "varchar(150)", "NO", "utf8mb4", "utf8mb4_0900_bin", ""),
        ("parent_checkpoint_id", "varchar(150)", "YES", "utf8mb4", "utf8mb4_0900_bin", ""),
        ("type", "varchar(150)", "YES", "utf8mb4", "utf8mb4_0900_bin", ""),
        ("checkpoint", "json", "NO", "", "", ""),
        ("metadata", "json", "NO", "", "", "DEFAULT_GENERATED"),
        ("checkpoint_ns_hash", "binary(16)", "NO", "", "", ""),
    ),
    "checkpoint_blobs": (
        ("thread_id", "varchar(150)", "NO", "utf8mb4", "utf8mb4_0900_bin", ""),
        ("checkpoint_ns", "varchar(2000)", "NO", "utf8mb4", "utf8mb4_0900_bin", ""),
        ("channel", "varchar(150)", "NO", "utf8mb4", "utf8mb4_0900_bin", ""),
        ("version", "varchar(150)", "NO", "utf8mb4", "utf8mb4_0900_bin", ""),
        ("type", "varchar(150)", "NO", "utf8mb4", "utf8mb4_0900_bin", ""),
        ("blob", "longblob", "YES", "", "", ""),
        ("checkpoint_ns_hash", "binary(16)", "NO", "", "", ""),
    ),
    "checkpoint_writes": (
        ("thread_id", "varchar(150)", "NO", "utf8mb4", "utf8mb4_0900_bin", ""),
        ("checkpoint_ns", "varchar(2000)", "NO", "utf8mb4", "utf8mb4_0900_bin", ""),
        ("checkpoint_id", "varchar(150)", "NO", "utf8mb4", "utf8mb4_0900_bin", ""),
        ("task_id", "varchar(150)", "NO", "utf8mb4", "utf8mb4_0900_bin", ""),
        ("idx", "int", "NO", "", "", ""),
        ("channel", "varchar(150)", "NO", "utf8mb4", "utf8mb4_0900_bin", ""),
        ("type", "varchar(150)", "YES", "utf8mb4", "utf8mb4_0900_bin", ""),
        ("blob", "longblob", "NO", "", "", ""),
        ("checkpoint_ns_hash", "binary(16)", "NO", "", "", ""),
        ("task_path", "varchar(2000)", "NO", "utf8mb4", "utf8mb4_0900_bin", ""),
    ),
    "checkpoint_migrations": (("v", "int", "NO", "", "", ""),),
    _LEDGER_TABLE: (
        ("source_digest", "char(64)", "NO", "utf8mb4", "utf8mb4_0900_bin", ""),
        ("status", "tinyint unsigned", "NO", "", "", ""),
        ("checkpoint_count", "bigint", "NO", "", "", ""),
        ("write_count", "bigint", "NO", "", "", ""),
        ("updated_at", "timestamp", "NO", "", "", "DEFAULT_GENERATED on update CURRENT_TIMESTAMP"),
    ),
}
_PRIMARY_KEYS = {
    "checkpoints": ("thread_id", "checkpoint_ns_hash", "checkpoint_id"),
    "checkpoint_blobs": ("thread_id", "checkpoint_ns_hash", "channel", "version"),
    "checkpoint_writes": ("thread_id", "checkpoint_ns_hash", "checkpoint_id", "task_id", "idx"),
    "checkpoint_migrations": ("v",),
    _LEDGER_TABLE: ("source_digest",),
}
_SECONDARY_INDEXES = {
    ("checkpoints", "checkpoints_checkpoint_id_idx"): ("checkpoint_id",),
    ("checkpoints", "checkpoints_thread_id_idx"): ("thread_id",),
    ("checkpoint_blobs", "checkpoint_blobs_thread_id_idx"): ("thread_id",),
    ("checkpoint_writes", "checkpoint_writes_thread_id_idx"): ("thread_id",),
}
_LEDGER_STATES = {
    "importing": 1,
    "complete": 2,
    "rolled-back": 3,
    "failed": 4,
    "cleanup-required": 5,
    "rolling-back": 6,
}


def _namespace_hash(value: str) -> bytes:
    return hashlib.md5(value.encode("utf-8"), usedforsecurity=False).digest()


@dataclass(frozen=True)
class _History:
    snapshot: Path
    groups: tuple[tuple[str, str], ...]
    tuples: tuple[Any, ...]
    write_rows: tuple[tuple[Any, ...], ...]
    threads: tuple[str, ...]
    source_digest: str

    @property
    def checkpoint_count(self) -> int:
        return len(self.tuples)

    @property
    def write_count(self) -> int:
        return len(self.write_rows)


def _installed_versions() -> None:
    from importlib.metadata import version

    if sys.version_info[:3] != (3, 13, 2):
        raise RuntimeError("checkpoint migration requires the Ticket01-validated Python 3.13.2 runtime")
    actual = {name: version(name) for name in _EXPECTED_PACKAGES}
    if actual != _EXPECTED_PACKAGES:
        raise RuntimeError("checkpoint migration requires the Ticket01-validated package versions")


def _sqlite_schema(connection: sqlite3.Connection) -> None:
    tables = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND substr(name,1,7) != 'sqlite_' COLLATE BINARY ORDER BY name"
    ).fetchall()
    names = {row[0] for row in tables}
    if names != set(_SQLITE_TABLES):
        raise ValueError("checkpoint source has unknown or missing tables")
    objects = connection.execute(
        "SELECT type,name FROM sqlite_master WHERE type IN ('view','trigger') ORDER BY type,name"
    ).fetchall()
    if objects:
        raise ValueError("checkpoint source contains unsupported views or triggers")
    for table, expected in _SQLITE_TABLES.items():
        columns = tuple(row[1] for row in connection.execute(f"PRAGMA table_info({table})"))
        if columns != expected:
            raise ValueError(f"checkpoint source table {table} does not match the frozen schema")
    result = connection.execute("PRAGMA integrity_check").fetchone()
    if result != ("ok",):
        raise ValueError("checkpoint source failed SQLite integrity_check")


def _json_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"$bytes": base64.b64encode(value).decode("ascii")}
    return value


def _snapshot(path: str | Path, directory: str) -> Path:
    source_path = Path(path).expanduser().resolve(strict=True)
    if not source_path.is_file():
        raise ValueError("checkpoint source must be a regular file")
    uri = source_path.as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=30)) as source:
        source.execute("PRAGMA query_only=ON")
        _sqlite_schema(source)
        destination_path = Path(directory) / "checkpoint-source.snapshot.sqlite"
        with closing(sqlite3.connect(str(destination_path), timeout=30)) as destination:
            source.backup(destination)
            destination.commit()
    return destination_path


async def _read_history(path: str | Path, directory: str) -> _History:
    _installed_versions()
    snapshot = _snapshot(path, directory)
    with closing(sqlite3.connect(f"file:{snapshot.as_posix()}?mode=ro", uri=True)) as connection:
        groups = tuple(
            (row[0], row[1])
            for row in connection.execute(
                "SELECT DISTINCT thread_id, checkpoint_ns FROM checkpoints ORDER BY thread_id, checkpoint_ns"
            )
        )
        raw_checkpoints = connection.execute(
            "SELECT thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, type, checkpoint, metadata "
            "FROM checkpoints ORDER BY thread_id, checkpoint_ns, checkpoint_id"
        ).fetchall()
        writes = connection.execute(
            "SELECT thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, type, value "
            "FROM writes ORDER BY thread_id, checkpoint_ns, checkpoint_id, task_id, idx"
        ).fetchall()

    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    tuples: list[Any] = []
    async with AsyncSqliteSaver.from_conn_string(str(snapshot)) as source:
        for thread_id, checkpoint_ns in groups:
            config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": checkpoint_ns}}
            tuples.extend([item async for item in source.alist(config)])
    tuples.sort(key=lambda item: (
        item.config["configurable"]["thread_id"],
        item.config["configurable"]["checkpoint_ns"],
        item.config["configurable"]["checkpoint_id"],
    ))
    _validate_history(groups, tuples, writes)

    logical_rows = [raw_checkpoints, writes]
    canonical = json.dumps(
        logical_rows,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_value,
    ).encode("utf-8")
    return _History(
        snapshot=snapshot,
        groups=groups,
        tuples=tuple(tuples),
        write_rows=tuple(tuple(row) for row in writes),
        threads=tuple(sorted({group[0] for group in groups})),
        source_digest=hashlib.sha256(canonical).hexdigest(),
    )


def _validate_history(groups: tuple[tuple[str, str], ...], tuples: list[Any], writes: list[Any]) -> None:
    by_key: dict[tuple[str, str, str], Any] = {}
    group_set = set(groups)
    for item in tuples:
        configurable = item.config["configurable"]
        key = (configurable["thread_id"], configurable["checkpoint_ns"], configurable["checkpoint_id"])
        if key in by_key:
            raise ValueError("checkpoint source has duplicate checkpoint identities")
        if (key[0], key[1]) not in group_set:
            raise ValueError("checkpoint source identity inventory is inconsistent")
        by_key[key] = item
    for key, item in by_key.items():
        if item.parent_config:
            parent = item.parent_config["configurable"]
            parent_key = (parent["thread_id"], parent["checkpoint_ns"], parent["checkpoint_id"])
            if parent_key not in by_key:
                raise ValueError("checkpoint source contains an unresolved parent")
            if parent_key[:2] != key[:2]:
                raise ValueError("checkpoint source parent crosses a thread or namespace")
        checkpoint = item.checkpoint
        for field in ("id", "channel_values", "channel_versions", "versions_seen"):
            if field not in checkpoint:
                raise ValueError("checkpoint source contains an incomplete checkpoint")
        if checkpoint["id"] != key[2]:
            raise ValueError("checkpoint source id disagrees with its public tuple")
        for value, maximum, field in (
            (key[0], 150, "thread_id"), (key[1], 2000, "checkpoint_ns"),
            (key[2], 150, "checkpoint_id"),
        ):
            if not isinstance(value, str) or len(value) > maximum:
                raise ValueError(f"checkpoint source {field} is not lossless in the validated MySQL schema")
        for channel in set(checkpoint["channel_values"]) | set(checkpoint["channel_versions"]):
            if not isinstance(channel, str) or len(channel) > 150:
                raise ValueError("checkpoint source channel is not lossless in the validated MySQL schema")
        if not isinstance(item.metadata, dict):
            raise ValueError("checkpoint source metadata is not a mapping")
    for row in writes:
        thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, type_, blob = row
        if (thread_id, checkpoint_ns, checkpoint_id) not in by_key:
            raise ValueError("checkpoint source contains a write without a checkpoint")
        for value, maximum, field in ((thread_id, 150, "thread_id"), (checkpoint_id, 150, "checkpoint_id"),
                                      (task_id, 150, "task_id"), (channel, 150, "channel"),
                                      (checkpoint_ns, 2000, "checkpoint_ns"), (type_, 150, "type")):
            if not isinstance(value, str) or len(value) > maximum:
                raise ValueError(f"checkpoint source {field} is not lossless in the validated MySQL schema")
        if not isinstance(idx, int) or not isinstance(blob, bytes):
            raise ValueError("checkpoint source contains an invalid pending write")


def _topological(tuples: tuple[Any, ...]) -> list[Any]:
    remaining = {
        (item.config["configurable"]["thread_id"], item.config["configurable"]["checkpoint_ns"], item.config["configurable"]["checkpoint_id"]): item
        for item in tuples
    }
    imported: set[tuple[str, str, str]] = set()
    ordered: list[Any] = []
    while remaining:
        ready = []
        for key, item in remaining.items():
            if not item.parent_config:
                ready.append(key)
            else:
                parent = item.parent_config["configurable"]
                parent_key = (parent["thread_id"], parent["checkpoint_ns"], parent["checkpoint_id"])
                if parent_key in imported:
                    ready.append(key)
        if not ready:
            raise ValueError("checkpoint source parent chain is cyclic")
        for key in sorted(ready):
            ordered.append(remaining.pop(key))
            imported.add(key)
    return ordered


def _new_versions(item: Any, parent: Any | None) -> dict[str, Any]:
    current = item.checkpoint["channel_versions"]
    previous = parent.checkpoint["channel_versions"] if parent else {}
    return {channel: version for channel, version in current.items() if previous.get(channel) != version}


class CheckpointHistoryMigration:
    """Preview, import, verify, rerun, and roll back one complete history."""

    def __init__(self, target_dsn: str | None):
        if target_dsn is not None:
            parsed = urlparse(target_dsn)
            if parsed.scheme != "mysql" or not parsed.hostname or not parsed.path.strip("/"):
                raise ValueError("checkpoint target must be an explicit MySQL DSN")
        self._dsn = target_dsn

    async def preview(self, source_path: str | Path) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="waygent-checkpoint-preview-") as directory:
            history = await _read_history(source_path, directory)
            return {
                "source_digest": history.source_digest,
                "checkpoint_count": history.checkpoint_count,
                "write_count": history.write_count,
                "thread_count": len(history.threads),
                "namespace_count": len(history.groups),
            }

    async def _connect(self):
        from importlib.metadata import version

        if self._dsn is None:
            raise RuntimeError("a MySQL target DSN is required for this operation")
        _installed_versions()
        if version("langgraph-checkpoint-mysql") != "3.0.0":
            raise RuntimeError("unvalidated checkpoint saver version")
        from app.runtime.checkpoint_mysql import GuardedAIOMySQLSaver

        return GuardedAIOMySQLSaver.from_conn_string(self._dsn)

    async def _lock(self):
        import aiomysql
        from app.runtime.checkpoint_mysql import GuardedAIOMySQLSaver

        if self._dsn is None:
            raise RuntimeError("a MySQL target DSN is required for this operation")
        params = GuardedAIOMySQLSaver.parse_conn_string(self._dsn)
        connection = await aiomysql.connect(**params, autocommit=True)
        lock_name = "waygent-checkpoint-import:" + hashlib.sha256(params["db"].encode()).hexdigest()[:32]
        try:
            async with connection.cursor() as cursor:
                await cursor.execute("SELECT GET_LOCK(%s, 30)", (lock_name,))
                if (await cursor.fetchone())[0] != 1:
                    raise TimeoutError("timed out waiting for the checkpoint import lock")
        except BaseException:
            connection.close()
            raise
        return connection, lock_name

    @asynccontextmanager
    async def _locked_connection(self):
        """Own the advisory-lock connection through operation and cleanup."""
        connection, lock_name = await self._lock()
        try:
            yield connection
        finally:
            try:
                async with connection.cursor() as cursor:
                    await cursor.execute("SELECT RELEASE_LOCK(%s)", (lock_name,))
            finally:
                # Driver errors or cancellation during RELEASE_LOCK must not
                # leave this session (and its advisory lock) alive.
                connection.close()

    async def _ensure_ledger(self, connection) -> None:
        async with connection.cursor() as cursor:
            await cursor.execute(
                f"CREATE TABLE IF NOT EXISTS {_LEDGER_TABLE} ("
                # Numeric state keeps the saver database's frozen VARCHAR
                # inventory identical to Ticket01's validated schema.
                "source_digest CHAR(64) PRIMARY KEY, status TINYINT UNSIGNED NOT NULL, "
                "checkpoint_count BIGINT NOT NULL, write_count BIGINT NOT NULL, "
                "updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP) "
                "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin"
            )
            await cursor.execute(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION",
                (_LEDGER_TABLE,),
            )
            columns = tuple(row[0] for row in await cursor.fetchall())
            if columns != ("source_digest", "status", "checkpoint_count", "write_count", "updated_at"):
                raise RuntimeError("checkpoint import ledger schema has drifted")
            await cursor.execute(
                "SELECT COLUMN_NAME,COLUMN_TYPE FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION",
                (_LEDGER_TABLE,),
            )
            types = tuple(await cursor.fetchall())
            if types != (
                ("source_digest", "char(64)"),
                ("status", "tinyint unsigned"),
                ("checkpoint_count", "bigint"),
                ("write_count", "bigint"),
                ("updated_at", "timestamp"),
            ):
                raise RuntimeError("checkpoint import ledger column types have drifted")
            await cursor.execute(
                "SELECT ENGINE,TABLE_COLLATION FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s",
                (_LEDGER_TABLE,),
            )
            table = await cursor.fetchone()
            if table != ("InnoDB", "utf8mb4_0900_bin"):
                raise RuntimeError("checkpoint import ledger engine or collation has drifted")

    async def _assert_mysql_schema(
        self, connection, *, allow_fresh: bool = False, allow_missing_ledger: bool = False
    ) -> None:
        async with connection.cursor() as cursor:
            await cursor.execute("SELECT VERSION()")
            if (await cursor.fetchone())[0] != "8.4.11":
                raise RuntimeError("checkpoint migration requires the Ticket01-validated MySQL 8.4.11 server")
            await cursor.execute(
                "SELECT TABLE_NAME,ENGINE,TABLE_COLLATION FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME IN "
                "('checkpoints','checkpoint_blobs','checkpoint_writes','checkpoint_migrations',%s) "
                "ORDER BY TABLE_NAME",
                (_LEDGER_TABLE,),
            )
            tables = await cursor.fetchall()
            names = {row[0] for row in tables}
            saver_names = _SAVER_TABLES
            if not names and allow_fresh:
                await cursor.execute(
                    "SELECT TABLE_NAME FROM information_schema.VIEWS WHERE TABLE_SCHEMA=DATABASE() "
                    "AND TABLE_NAME IN ('checkpoints','checkpoint_blobs','checkpoint_writes','checkpoint_migrations',%s)",
                    (_LEDGER_TABLE,),
                )
                if await cursor.fetchall():
                    raise RuntimeError("checkpoint target has a view that conflicts with the frozen saver schema")
                await cursor.execute(
                    "SELECT EVENT_OBJECT_TABLE FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=DATABASE() "
                    "AND EVENT_OBJECT_TABLE IN ('checkpoints','checkpoint_blobs','checkpoint_writes','checkpoint_migrations',%s)",
                    (_LEDGER_TABLE,),
                )
                if await cursor.fetchall():
                    raise RuntimeError("checkpoint target has an unsafe trigger")
                await cursor.execute(
                    "SELECT PAD_ATTRIBUTE FROM information_schema.COLLATIONS "
                    "WHERE COLLATION_NAME='utf8mb4_0900_bin'"
                )
                if await cursor.fetchone() != ("NO PAD",):
                    raise RuntimeError("checkpoint saver requires the validated NO PAD collation")
                return
            if allow_missing_ledger:
                allowed = {frozenset(saver_names), frozenset(saver_names | {_LEDGER_TABLE})}
                if frozenset(names) not in allowed:
                    raise RuntimeError("checkpoint target has a partial or unexpected frozen saver schema")
                required_names = names
            else:
                required_names = saver_names | {_LEDGER_TABLE}
            if names != required_names:
                raise RuntimeError("checkpoint target has a partial or unexpected frozen saver schema")
            if tables != tuple((name, "InnoDB", "utf8mb4_0900_bin") for name in sorted(required_names)):
                raise RuntimeError("checkpoint saver tables do not match the validated MySQL engine/collation")
            await cursor.execute(
                "SELECT TABLE_NAME,COLUMN_NAME,COLUMN_TYPE,IS_NULLABLE,CHARACTER_SET_NAME,COLLATION_NAME,EXTRA "
                "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() "
                "AND TABLE_NAME IN ('checkpoints','checkpoint_blobs','checkpoint_writes','checkpoint_migrations',%s) "
                "ORDER BY TABLE_NAME,ORDINAL_POSITION",
                (_LEDGER_TABLE,),
            )
            actual_columns = {table: [] for table in required_names}
            for table_name, column_name, column_type, nullable, charset, collation, extra in await cursor.fetchall():
                actual_columns[table_name].append(
                    (column_name, column_type, nullable, charset or "", collation or "", extra or "")
                )
            expected_columns = {table: _TABLE_LAYOUTS[table] for table in required_names}
            if {table: tuple(rows) for table, rows in actual_columns.items()} != expected_columns:
                raise RuntimeError("checkpoint saver columns, types, nullability, JSON/blob layout, or collation drifted")
            await cursor.execute(
                "SELECT TABLE_NAME,CONSTRAINT_NAME,CONSTRAINT_TYPE FROM information_schema.TABLE_CONSTRAINTS "
                "WHERE CONSTRAINT_SCHEMA=DATABASE() AND TABLE_NAME IN "
                "('checkpoints','checkpoint_blobs','checkpoint_writes','checkpoint_migrations',%s) "
                "ORDER BY TABLE_NAME,CONSTRAINT_NAME",
                (_LEDGER_TABLE,),
            )
            actual_constraints = tuple(await cursor.fetchall())
            expected_constraints = tuple(
                (table, "PRIMARY", "PRIMARY KEY") for table in sorted(required_names)
            )
            if actual_constraints != expected_constraints:
                raise RuntimeError("checkpoint saver constraints differ from the frozen primary-key layout")
            await cursor.execute(
                "SELECT TABLE_NAME,INDEX_NAME,NON_UNIQUE,SEQ_IN_INDEX,COLUMN_NAME,SUB_PART,INDEX_TYPE "
                "FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME IN "
                "('checkpoints','checkpoint_blobs','checkpoint_writes','checkpoint_migrations',%s) "
                "ORDER BY TABLE_NAME,INDEX_NAME,SEQ_IN_INDEX",
                (_LEDGER_TABLE,),
            )
            actual_indexes = tuple(await cursor.fetchall())
            expected_indexes = []
            for table, columns in _PRIMARY_KEYS.items():
                if table in required_names:
                    expected_indexes.extend(
                        (table, "PRIMARY", 0, position, column, None, "BTREE")
                        for position, column in enumerate(columns, 1)
                    )
            for (table, index_name), columns in _SECONDARY_INDEXES.items():
                if table in required_names:
                    expected_indexes.extend(
                        (table, index_name, 1, position, column, None, "BTREE")
                        for position, column in enumerate(columns, 1)
                    )
            if len(actual_indexes) != len(expected_indexes) or set(actual_indexes) != set(expected_indexes):
                raise RuntimeError("checkpoint saver indexes differ from the frozen index layout")
            await cursor.execute(
                "SELECT TRIGGER_NAME FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=DATABASE() "
                "AND EVENT_OBJECT_TABLE IN ('checkpoints','checkpoint_blobs','checkpoint_writes','checkpoint_migrations',%s)",
                (_LEDGER_TABLE,),
            )
            if await cursor.fetchall():
                raise RuntimeError("checkpoint target has an unsafe trigger")
            await cursor.execute(
                "SELECT PAD_ATTRIBUTE FROM information_schema.COLLATIONS "
                "WHERE COLLATION_NAME='utf8mb4_0900_bin'"
            )
            if await cursor.fetchone() != ("NO PAD",):
                raise RuntimeError("checkpoint saver requires the validated NO PAD collation")

    async def verify_runtime_ready(self) -> None:
        """Read-only startup gate for the frozen saver and imported history."""
        async with self._locked_connection() as connection:
            await self._assert_mysql_schema(connection)
            async with connection.cursor() as cursor:
                await cursor.execute(
                    f"SELECT COUNT(*) FROM {_LEDGER_TABLE} WHERE status=%s",
                    (_LEDGER_STATES["complete"],),
                )
                if (await cursor.fetchone())[0] < 1:
                    raise RuntimeError("checkpoint MySQL history has no completed import record")

    async def _prepare_saver(self, saver, connection) -> None:
        # A completely fresh saver database is allowed after server/default
        # collation preflight. Any existing partial/drifted saver schema fails
        # before setup() can execute DDL.
        await self._assert_mysql_schema(connection, allow_fresh=True, allow_missing_ledger=True)
        await saver.setup()
        await self._ensure_ledger(connection)
        await self._assert_mysql_schema(connection)

    async def _target_counts(self, connection, threads: tuple[str, ...] | None = None) -> tuple[int, int, int]:
        async with connection.cursor() as cursor:
            if threads is None:
                await cursor.execute("SELECT COUNT(*) FROM checkpoints")
                checkpoints = (await cursor.fetchone())[0]
                await cursor.execute("SELECT COUNT(*) FROM checkpoint_writes")
                writes = (await cursor.fetchone())[0]
                await cursor.execute("SELECT COUNT(*) FROM checkpoint_blobs")
                blobs = (await cursor.fetchone())[0]
                return checkpoints, writes, blobs
            if not threads:
                return 0, 0, 0
            marks = ",".join("%s" for _ in threads)
            await cursor.execute(f"SELECT COUNT(*) FROM checkpoints WHERE thread_id IN ({marks})", threads)
            checkpoints = (await cursor.fetchone())[0]
            await cursor.execute(f"SELECT COUNT(*) FROM checkpoint_writes WHERE thread_id IN ({marks})", threads)
            writes = (await cursor.fetchone())[0]
            await cursor.execute(f"SELECT COUNT(*) FROM checkpoint_blobs WHERE thread_id IN ({marks})", threads)
            blobs = (await cursor.fetchone())[0]
            return checkpoints, writes, blobs

    async def _ledger_row(self, connection, digest: str):
        async with connection.cursor() as cursor:
            await cursor.execute(
                f"SELECT status, checkpoint_count, write_count FROM {_LEDGER_TABLE} WHERE source_digest=%s",
                (digest,),
            )
            row = await cursor.fetchone()
            if row:
                state = {value: key for key, value in _LEDGER_STATES.items()}.get(row[0])
                if state is None:
                    raise RuntimeError("checkpoint import ledger contains an unknown state")
                return (state, row[1], row[2])
            return None

    async def _write_state(self, connection, history: _History, status: str) -> None:
        try:
            code = _LEDGER_STATES[status]
        except KeyError as exc:
            raise ValueError("unknown checkpoint import state") from exc
        async with connection.cursor() as cursor:
            await cursor.execute(
                f"INSERT INTO {_LEDGER_TABLE} (source_digest,status,checkpoint_count,write_count) VALUES (%s,%s,%s,%s) AS incoming "
                "ON DUPLICATE KEY UPDATE status=incoming.status, checkpoint_count=incoming.checkpoint_count, write_count=incoming.write_count",
                (history.source_digest, code, history.checkpoint_count, history.write_count),
            )

    async def _source_write_rows(self, connection, history: _History) -> None:
        if not history.write_rows:
            return
        rows = [
            (thread_id, checkpoint_ns, checkpoint_ns, checkpoint_id, task_id, "", idx, channel, type_, blob)
            for thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, type_, blob in history.write_rows
        ]
        async with connection.cursor() as cursor:
            await cursor.executemany(
                "INSERT INTO checkpoint_writes "
                "(thread_id,checkpoint_ns,checkpoint_ns_hash,checkpoint_id,task_id,task_path,idx,channel,type,`blob`) "
                "VALUES (%s,%s,UNHEX(MD5(%s)),%s,%s,%s,%s,%s,%s,%s)",
                rows,
            )
        await connection.commit()

    async def _import(self, saver, history: _History) -> None:
        by_key = {
            (item.config["configurable"]["thread_id"], item.config["configurable"]["checkpoint_ns"], item.config["configurable"]["checkpoint_id"]): item
            for item in history.tuples
        }
        for item in _topological(history.tuples):
            key = (item.config["configurable"]["thread_id"], item.config["configurable"]["checkpoint_ns"], item.config["configurable"]["checkpoint_id"])
            parent = None
            if item.parent_config:
                p = item.parent_config["configurable"]
                parent = by_key[(p["thread_id"], p["checkpoint_ns"], p["checkpoint_id"])]
            base = item.parent_config or {
                "configurable": {
                    key_name: value
                    for key_name, value in item.config["configurable"].items()
                    if key_name != "checkpoint_id"
                }
            }
            await saver.aput(base, item.checkpoint, item.metadata, _new_versions(item, parent))

    async def _read_target(self, saver, history: _History) -> list[Any]:
        result = []
        for thread_id, checkpoint_ns in history.groups:
            config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": checkpoint_ns}}
            result.extend([item async for item in saver.alist(config)])
        result.sort(key=lambda item: (
            item.config["configurable"]["thread_id"],
            item.config["configurable"]["checkpoint_ns"],
            item.config["configurable"]["checkpoint_id"],
        ))
        return result

    async def _compare_write_rows(self, connection, history: _History, *, subset: bool = False) -> bool:
        if not history.threads:
            return not history.write_rows
        marks = ",".join("%s" for _ in history.threads)
        async with connection.cursor() as cursor:
            await cursor.execute(
                f"SELECT thread_id,checkpoint_ns,checkpoint_ns_hash,checkpoint_id,task_id,idx,channel,type,`blob`,task_path "
                f"FROM checkpoint_writes WHERE thread_id IN ({marks}) "
                "ORDER BY thread_id,checkpoint_ns,checkpoint_id,task_id,idx",
                history.threads,
            )
            actual = [tuple(row) for row in await cursor.fetchall()]
        expected = {
            (thread_id, namespace, _namespace_hash(namespace), checkpoint_id, task_id, idx, channel, type_, blob, "")
            for thread_id, namespace, checkpoint_id, task_id, idx, channel, type_, blob in history.write_rows
        }
        return set(actual).issubset(expected) if subset else (len(actual) == len(expected) and set(actual) == expected)

    def _expected_blob_rows(self, saver, history: _History) -> list[tuple[Any, ...]]:
        expected: dict[tuple[str, str, str, str], tuple[str, bytes | None]] = {}
        for item in history.tuples:
            configurable = item.config["configurable"]
            thread_id = configurable["thread_id"]
            checkpoint_ns = configurable["checkpoint_ns"]
            values = item.checkpoint["channel_values"]
            for channel, version in item.checkpoint["channel_versions"].items():
                value_key = (thread_id, checkpoint_ns, channel, str(version))
                if channel in values and (
                    values[channel] is None
                    or isinstance(values[channel], (str, int, float, bool))
                ):
                    continue
                if channel not in values:
                    # The pinned MySQL saver only persists a blob when the
                    # channel value is present; channel_versions may include
                    # a newly-versioned channel without a value.
                    continue
                type_, blob = saver.serde.dumps_typed(values[channel])
                previous = expected.setdefault(value_key, (type_, blob))
                if previous != (type_, blob):
                    raise ValueError("checkpoint source reuses a channel version for different serialized values")
        return sorted(
            [(thread, namespace, _namespace_hash(namespace), channel, version, type_, blob)
             for (thread, namespace, channel, version), (type_, blob) in expected.items()],
            key=lambda row: row[:4],
        )

    async def _compare_blob_rows(self, connection, saver, history: _History, *, subset: bool = False) -> bool:
        expected = self._expected_blob_rows(saver, history)
        if not history.threads:
            return not expected
        marks = ",".join("%s" for _ in history.threads)
        async with connection.cursor() as cursor:
            await cursor.execute(
                f"SELECT thread_id,checkpoint_ns,checkpoint_ns_hash,channel,version,type,`blob` "
                f"FROM checkpoint_blobs WHERE thread_id IN ({marks}) "
                "ORDER BY thread_id,checkpoint_ns,channel,version",
                history.threads,
            )
            actual = [tuple(row) for row in await cursor.fetchall()]
        return set(actual).issubset(set(expected)) if subset else (len(actual) == len(expected) and set(actual) == set(expected))

    async def _compare_checkpoint_storage(self, connection, saver, history: _History, *, subset: bool = False) -> bool:
        if not history.threads:
            return not history.tuples
        marks = ",".join("%s" for _ in history.threads)
        async with connection.cursor() as cursor:
            await cursor.execute(
                f"SELECT thread_id,checkpoint_ns,checkpoint_ns_hash,checkpoint_id,parent_checkpoint_id,type "
                f"FROM checkpoints WHERE thread_id IN ({marks})",
                history.threads,
            )
            actual = [tuple(row) for row in await cursor.fetchall()]
        expected = set()
        for item in history.tuples:
            configurable = item.config["configurable"]
            parent_id = None
            if item.parent_config:
                parent_id = item.parent_config["configurable"]["checkpoint_id"]
            namespace = configurable["checkpoint_ns"]
            expected.add((
                configurable["thread_id"], namespace, _namespace_hash(namespace),
                configurable["checkpoint_id"], parent_id, None,
            ))
        return set(actual).issubset(expected) if subset else (len(actual) == len(expected) and set(actual) == expected)

    async def _remaining_rows_are_source_subset(self, saver, connection, history: _History) -> bool:
        # The target is dedicated to this import. Any row under another thread
        # or any modified row inside its thread identities blocks cleanup.
        if await self._target_counts(connection) != await self._target_counts(connection, history.threads):
            return False
        expected = {
            (item.config["configurable"]["thread_id"],
             item.config["configurable"]["checkpoint_ns"],
             item.config["configurable"]["checkpoint_id"]): item
            for item in history.tuples
        }
        actual = await self._read_target(saver, history)
        actual_by_key = {}
        for item in actual:
            cfg = item.config["configurable"]
            key = (cfg["thread_id"], cfg["checkpoint_ns"], cfg["checkpoint_id"])
            source_item = expected.get(key)
            if key in actual_by_key or source_item is None:
                return False
            if (
                item.config != source_item.config
                or item.checkpoint != source_item.checkpoint
                or item.metadata != source_item.metadata
                or item.parent_config != source_item.parent_config
                or any(write not in source_item.pending_writes for write in item.pending_writes)
            ):
                return False
            actual_by_key[key] = item
        if not await self._compare_checkpoint_storage(connection, saver, history, subset=True):
            return False
        if not await self._compare_write_rows(connection, history, subset=True):
            return False
        if not await self._compare_blob_rows(connection, saver, history, subset=True):
            return False
        return True

    async def _compare(self, saver, connection, history: _History) -> bool:
        actual = await self._read_target(saver, history)
        if list(history.tuples) != actual:
            return False
        if await self._target_counts(connection) != await self._target_counts(connection, history.threads):
            return False
        if not await self._compare_checkpoint_storage(connection, saver, history):
            return False
        if not await self._compare_write_rows(connection, history):
            return False
        if not await self._compare_blob_rows(connection, saver, history):
            return False
        return True

    async def _delete_threads(self, saver, threads: tuple[str, ...]) -> None:
        for thread_id in threads:
            await saver.adelete_thread(thread_id)

    async def apply(self, source_path: str | Path, *, checkpoint_writes_frozen: bool) -> dict[str, Any]:
        if not checkpoint_writes_frozen:
            raise ValueError("checkpoint import requires an explicitly confirmed joint checkpoint write freeze")
        with tempfile.TemporaryDirectory(prefix="waygent-checkpoint-import-") as directory:
            history = await _read_history(source_path, directory)
            if not history.tuples:
                raise ValueError("checkpoint source is empty")
            async with self._locked_connection() as lock_connection:
                cleanup_needed = False
                try:
                    async with await self._connect() as saver:
                        await self._prepare_saver(saver, lock_connection)
                        self._expected_blob_rows(saver, history)
                        existing = await self._ledger_row(lock_connection, history.source_digest)
                        if existing and (existing[1], existing[2]) != (history.checkpoint_count, history.write_count):
                            raise RuntimeError("checkpoint import ledger manifest counts do not match the source digest")
                        if existing and existing[0] == "complete":
                            if not await self._compare(saver, lock_connection, history):
                                raise RuntimeError("completed checkpoint import no longer matches its source")
                            return {"status": "already-imported", "checkpoint_count": history.checkpoint_count, "write_count": history.write_count}
                        counts = await self._target_counts(lock_connection)
                        if existing is None and any(counts):
                            raise RuntimeError("checkpoint target is not empty; refusing an unsafe merge")
                        if existing and existing[0] == "rolling-back":
                            raise RuntimeError("checkpoint rollback is in progress; apply is refused")
                        if existing and existing[0] in {"rolled-back", "failed"} and any(counts):
                            raise RuntimeError("checkpoint target is not empty; refusing to overwrite history after rollback or failure")
                        if existing and existing[0] not in {"rolled-back", "failed", "importing", "cleanup-required"}:
                            raise RuntimeError("checkpoint import ledger is in an unexpected state")
                        await self._write_state(lock_connection, history, "importing")
                        cleanup_needed = True
                        if existing and existing[0] in {"importing", "cleanup-required"}:
                            if not await self._remaining_rows_are_source_subset(saver, lock_connection, history):
                                raise RuntimeError("partial checkpoint import has rows that do not match its source manifest")
                            await self._delete_threads(saver, history.threads)
                        if any(await self._target_counts(lock_connection)):
                            raise RuntimeError("checkpoint target contains rows outside this import; refusing to merge")
                        await self._import(saver, history)
                        await self._source_write_rows(lock_connection, history)
                        if not await self._compare(saver, lock_connection, history):
                            raise RuntimeError("checkpoint import verification did not match the source")
                        await self._write_state(lock_connection, history, "complete")
                        cleanup_needed = False
                        return {"status": "complete", "checkpoint_count": history.checkpoint_count, "write_count": history.write_count}
                except BaseException:
                    if cleanup_needed:
                        try:
                            # Context managers returned by from_conn_string are
                            # single-use; cleanup must own a fresh connection.
                            async with await self._connect() as saver:
                                if await self._remaining_rows_are_source_subset(saver, lock_connection, history):
                                    await self._delete_threads(saver, history.threads)
                                else:
                                    raise RuntimeError("target rows changed during failed import cleanup")
                            await self._write_state(lock_connection, history, "failed")
                        except BaseException:
                            await self._write_state(lock_connection, history, "cleanup-required")
                    raise

    async def verify(self, source_path: str | Path) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="waygent-checkpoint-verify-") as directory:
            history = await _read_history(source_path, directory)
            async with self._locked_connection() as lock_connection:
                async with await self._connect() as saver:
                    await self._prepare_saver(saver, lock_connection)
                    counts = await self._target_counts(lock_connection, history.threads)
                    matches = await self._compare(saver, lock_connection, history)
                    return {"matches": matches, "checkpoint_count": counts[0], "write_count": counts[1], "blob_count": counts[2]}

    async def read_target(self, source_path: str | Path) -> list[Any]:
        """Public saver tuples for integration verification; source identifiers are not logged."""
        with tempfile.TemporaryDirectory(prefix="waygent-checkpoint-read-") as directory:
            history = await _read_history(source_path, directory)
            async with await self._connect() as saver:
                await self._prepare_saver(saver, saver.conn)
                return await self._read_target(saver, history)

    async def rollback(self, source_path: str | Path, *, checkpoint_writes_frozen: bool) -> dict[str, Any]:
        if not checkpoint_writes_frozen:
            raise ValueError("checkpoint rollback requires an explicitly confirmed joint checkpoint write freeze")
        with tempfile.TemporaryDirectory(prefix="waygent-checkpoint-rollback-") as directory:
            history = await _read_history(source_path, directory)
            async with self._locked_connection() as lock_connection:
                async with await self._connect() as saver:
                    await self._prepare_saver(saver, lock_connection)
                    existing = await self._ledger_row(lock_connection, history.source_digest)
                    if not existing or (existing[1], existing[2]) != (history.checkpoint_count, history.write_count):
                        raise RuntimeError("no matching checkpoint import manifest is available to roll back")
                    state = existing[0]
                    if state == "rolled-back":
                        if any(await self._target_counts(lock_connection, history.threads)):
                            raise RuntimeError("checkpoint target changed after rollback; refusing to delete history")
                        return {"status": "rolled-back", "checkpoint_count": 0, "write_count": 0}
                    if state not in {"complete", "failed", "importing", "cleanup-required", "rolling-back"}:
                        raise RuntimeError("no matching checkpoint import is available to roll back")
                    if state == "complete":
                        if not await self._compare(saver, lock_connection, history):
                            raise RuntimeError("checkpoint target has changed since import; refusing rollback")
                        await self._write_state(lock_connection, history, "rolling-back")
                    elif not await self._remaining_rows_are_source_subset(saver, lock_connection, history):
                        raise RuntimeError("checkpoint target changed during rollback; refusing to delete history")
                    await self._delete_threads(saver, history.threads)
                    counts = await self._target_counts(lock_connection, history.threads)
                    if any(counts):
                        raise RuntimeError("checkpoint rollback left target rows behind")
                    await self._write_state(lock_connection, history, "rolled-back")
                    return {"status": "rolled-back", "checkpoint_count": 0, "write_count": 0}

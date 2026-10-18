"""Fail-closed, transactional import of SQLite business history to staged MySQL.

Import is intentionally explicit. Call ``upgrade_mysql_schema`` separately to
initialize an empty target; these functions never run migrations or switch app
traffic. Use a stopped application and an offline SQLite backup for real data.
"""

from __future__ import annotations

import base64
import argparse
import hashlib
import json
import os
import sqlite3
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator
from urllib.parse import quote

from sqlalchemy import insert, inspect, select, text

from app.core.business_history_import_schema import (
    IMPORT_TABLES,
    SOURCE_TABLES,
    build_business_import_metadata,
    import_ledger,
)


@dataclass(frozen=True)
class ImportManifest:
    source_id: str
    source_digest: str
    table_counts: dict[str, int]
    table_hashes: dict[str, str]
    omitted_table_counts: dict[str, int]
    omitted_table_presence: dict[str, bool]
    already_applied: bool = False

    def as_dict(self) -> dict:
        return {
            "source_id": self.source_id,
            "source_digest": self.source_digest,
            "table_counts": dict(sorted(self.table_counts.items())),
            "table_hashes": dict(sorted(self.table_hashes.items())),
            "omitted_table_counts": dict(sorted(self.omitted_table_counts.items())),
            "omitted_table_presence": dict(sorted(self.omitted_table_presence.items())),
            "already_applied": self.already_applied,
        }


class BusinessImportError(RuntimeError):
    """Source, target, or verification contract failed closed."""


def _source_id(path: Path) -> str:
    identity = str(path.resolve()).replace("\\", "/").casefold()
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _jsonable(value):
    if isinstance(value, bytes):
        return {"$bytes": base64.b64encode(value).decode("ascii")}
    return value


def _canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=_jsonable).encode("utf-8")


def _readonly_source(path: str | Path) -> sqlite3.Connection:
    source = Path(path).resolve()
    if not source.is_file():
        raise BusinessImportError("SQLite source file does not exist")
    uri = "file:" + quote(source.as_posix(), safe="/:\\") + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("BEGIN")
    return connection


def _source_inventory(source: sqlite3.Connection):
    objects = source.execute(
        "SELECT type,name FROM sqlite_master WHERE substr(name,1,7) != 'sqlite_' COLLATE BINARY ORDER BY type,name"
    ).fetchall()
    tables = {str(row["name"]) for row in objects if row["type"] == "table"}
    unexpected_objects = [row["name"] for row in objects if row["type"] not in {"table", "index"}]
    unknown = tables - SOURCE_TABLES - {"pending_modifications", "alembic_version"}
    if unknown or unexpected_objects:
        names = sorted(unknown | set(unexpected_objects))
        raise BusinessImportError("unknown source table or object: " + ", ".join(names))
    missing = SOURCE_TABLES - tables
    if missing:
        raise BusinessImportError("source business tables missing: " + ", ".join(sorted(missing)))
    pending_count = 0
    if "pending_modifications" in tables:
        pending_count = int(source.execute('SELECT COUNT(*) FROM "pending_modifications"').fetchone()[0])
        if pending_count:
            raise BusinessImportError("pending_modifications contains rows; convert or resolve them before import")
    violations = source.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise BusinessImportError(f"source has {len(violations)} foreign-key violation(s)")
    metadata = build_business_import_metadata()
    rows_by_table: dict[str, list[dict]] = {}
    counts: dict[str, int] = {}
    hashes: dict[str, str] = {}
    for name in IMPORT_TABLES:
        table = metadata.tables[name]
        source_columns = {str(row["name"]) for row in source.execute(f'PRAGMA table_info("{name}")')}
        expected_columns = {column.name for column in table.columns if not (name == "users" and column.name == "username_digest")}
        if source_columns != expected_columns:
            raise BusinessImportError(f"source schema mismatch for {name}; missing={sorted(expected_columns-source_columns)}, extra={sorted(source_columns-expected_columns)}")
        rows = [dict(row) for row in source.execute(f'SELECT * FROM "{name}"')]
        pk_columns = [str(row["name"]) for row in source.execute(f'PRAGMA table_info("{name}")') if int(row["pk"]) > 0]
        rows.sort(key=lambda row: tuple(str(row[key]) for key in pk_columns))
        for row in rows:
            _validate_json_fields(name, row)
        digest = hashlib.sha256()
        for row in rows:
            digest.update(_canonical(row))
            digest.update(b"\n")
        rows_by_table[name] = rows
        counts[name] = len(rows)
        hashes[name] = digest.hexdigest()
    _validate_source_relationships(rows_by_table)
    return rows_by_table, counts, hashes, {"pending_modifications": pending_count}, {"pending_modifications": "pending_modifications" in tables}


def _validate_json_fields(table: str, row: dict) -> None:
    for name, value in row.items():
        if value is None or not (name.endswith("_json") or name == "scope_key"):
            continue
        try:
            json.loads(value)
        except (TypeError, json.JSONDecodeError) as exc:
            raise BusinessImportError(f"malformed JSON at {table}.{name}") from exc


def _validate_source_relationships(rows: dict[str, list[dict]]) -> None:
    ids = {
        name: {row["id"] for row in rows[name]}
        for name in IMPORT_TABLES
        if rows[name] and "id" in rows[name][0]
    }
    # These application-level relationships are not all physical SQLite FKs,
    # but must resolve before the importer can claim referential parity.
    references = (
        ("itineraries", "parent_id", "itineraries"),
        ("itineraries", "root_id", "itineraries"),
        ("messages", "related_run_id", "runs"),
        ("messages", "related_itinerary_id", "itineraries"),
        ("planning_briefs", "submitted_run_id", "runs"),
        ("runs", "retry_of_run_id", "runs"),
        ("runs", "result_itinerary_id", "itineraries"),
        ("memory_facts", "supersedes_id", "memory_facts"),
        ("run_interaction_responses", "run_id", "runs"),
        ("run_interaction_responses", "message_id", "messages"),
    )
    for table, column, target in references:
        target_ids = ids.get(target, set())
        if any(row.get(column) is not None and row[column] not in target_ids for row in rows[table]):
            raise BusinessImportError(f"source has unresolved reference {table}.{column}")
    _validate_source_ownership(rows)


def _validate_source_ownership(rows: dict[str, list[dict]]) -> None:
    """Validate every semantic cross-row reference against its owning account."""
    owners = {
        table: {row["id"]: row["user_id"] for row in rows[table]}
        for table in ("conversations", "itineraries", "runs", "messages", "memory_facts")
    }

    def require_owner(table: str, column: str, parent_table: str, parent_id, child_owner) -> None:
        if parent_id is not None and (
            parent_id not in owners[parent_table] or owners[parent_table][parent_id] != child_owner
        ):
            raise BusinessImportError(f"source ownership reference mismatch in {table}.{column}")

    for row in rows["itineraries"]:
        require_owner("itineraries", "parent_id", "itineraries", row.get("parent_id"), row["user_id"])
        require_owner("itineraries", "root_id", "itineraries", row.get("root_id"), row["user_id"])
    for table, column in (
        ("messages", "conversation_id"), ("planning_briefs", "conversation_id"),
        ("conversation_memory_states", "conversation_id"), ("memory_extraction_jobs", "conversation_id"),
    ):
        for row in rows[table]:
            require_owner(table, column, "conversations", row.get(column), row["user_id"])
    for row in rows["messages"]:
        require_owner("messages", "related_run_id", "runs", row.get("related_run_id"), row["user_id"])
        require_owner("messages", "related_itinerary_id", "itineraries", row.get("related_itinerary_id"), row["user_id"])
    for row in rows["planning_briefs"]:
        submitted = row.get("submitted_run_id")
        require_owner("planning_briefs", "submitted_run_id", "runs", submitted, row["user_id"])
        if submitted is not None:
            run = next(item for item in rows["runs"] if item["id"] == submitted)
            if run.get("conversation_id") != row["conversation_id"]:
                raise BusinessImportError("source ownership reference mismatch in planning_briefs.submitted_run_id")
    for row in rows["runs"]:
        require_owner("runs", "conversation_id", "conversations", row.get("conversation_id"), row["user_id"])
        require_owner("runs", "retry_of_run_id", "runs", row.get("retry_of_run_id"), row["user_id"])
        require_owner("runs", "result_itinerary_id", "itineraries", row.get("result_itinerary_id"), row["user_id"])
    for row in rows["memory_facts"]:
        require_owner("memory_facts", "source_conversation_id", "conversations", row.get("source_conversation_id"), row["user_id"])
        require_owner("memory_facts", "supersedes_id", "memory_facts", row.get("supersedes_id"), row["user_id"])
    for table in ("itinerary_enrichments", "optimizer_shadow_runs"):
        for row in rows[table]:
            require_owner(table, "itinerary_id", "itineraries", row.get("itinerary_id"), row["user_id"])
    for row in rows["run_interaction_responses"]:
        require_owner("run_interaction_responses", "run_id", "runs", row.get("run_id"), owners["runs"].get(row.get("run_id")))
        require_owner("run_interaction_responses", "message_id", "messages", row.get("message_id"), owners["runs"].get(row.get("run_id")))


def _manifest(path: Path, counts: dict[str, int], hashes: dict[str, str], omitted: dict[str, int], omitted_presence: dict[str, bool]) -> ImportManifest:
    source_id = _source_id(path)
    digest_doc = {
        "tables": {name: {"count": counts[name], "sha256": hashes[name]} for name in sorted(counts)},
        "omitted": dict(sorted(omitted.items())),
        "omitted_presence": dict(sorted(omitted_presence.items())),
    }
    return ImportManifest(source_id, hashlib.sha256(_canonical(digest_doc)).hexdigest(), counts, hashes, omitted, omitted_presence)


def _table_rows(source: sqlite3.Connection, name: str) -> Iterator[dict]:
    cursor = source.execute(f'SELECT * FROM "{name}"')
    for row in cursor:
        yield dict(row)


def _assert_mysql_schema(database) -> None:
    if database.engine.dialect.name != "mysql":
        raise BusinessImportError("business-history import requires MySQL")
    with database.transaction() as conn:
        _validate_mysql_schema(conn)


def _validate_mysql_schema(conn) -> None:
    from alembic.runtime.migration import MigrationContext
    from app.core.mysql_schema_checks import validate_existing
    expected = build_business_import_metadata()
    try:
        if MigrationContext.configure(conn).get_current_revision() != "20261006_mysql_06":
            raise BusinessImportError("initialize the empty MySQL schema to revision 06 before importing")
        actual = set(inspect(conn).get_table_names())
        if actual != set(expected.tables) | {"alembic_version"}:
            raise BusinessImportError("MySQL schema differs from the frozen revision 06 table inventory")
        validate_existing(conn, expected)
    except BusinessImportError:
        raise
    except Exception as exc:
        raise BusinessImportError("MySQL schema differs from the frozen revision 06 contract") from exc


def _assert_empty_schema_target(database) -> None:
    """Guard the separate schema-init CLI from accidental live-data targets."""
    _assert_mysql_schema_type(database)
    metadata = build_business_import_metadata()
    with database.transaction() as conn:
        actual = set(inspect(conn).get_table_names()) - {"alembic_version"}
        if actual - set(metadata.tables):
            raise BusinessImportError("schema initialization target contains unknown tables")
        for name in actual:
            if conn.execute(select(text("COUNT(*)")).select_from(metadata.tables[name])).scalar_one():
                raise BusinessImportError("schema initialization requires an empty MySQL target")


def _assert_mysql_schema_type(database) -> None:
    if database.engine.dialect.name != "mysql":
        raise BusinessImportError("business-history import requires MySQL")


@contextmanager
def _import_lock(database):
    """Serialize import/rollback operators targeting the same MySQL schema."""
    database_name = str(database.engine.url.database)
    name = "waygent-business-import-" + hashlib.sha256(database_name.encode("utf-8")).hexdigest()[:32]
    with database.engine.connect() as connection:
        acquired = connection.execute(text("SELECT GET_LOCK(:name, 30)"), {"name": name}).scalar_one()
        if acquired != 1:
            raise BusinessImportError("MySQL import lock unavailable")
        connection.commit()
        try:
            yield connection
        finally:
            connection.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": name})
            connection.commit()


def _target_ledger(conn, manifest: ImportManifest):
    ledger_rows = conn.execute(select(import_ledger)).mappings().all()
    matching = [row for row in ledger_rows if row["source_id"] == manifest.source_id]
    if len(ledger_rows) > 1 or (ledger_rows and len(matching) != 1):
        raise BusinessImportError("target contains extra or unrelated business import ledger rows")
    row = matching[0] if matching else None
    if row is not None and row["source_digest"] != manifest.source_digest:
        raise BusinessImportError("source identity already imported with different content")
    if row is not None:
        try:
            stored = json.loads(row["manifest_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise BusinessImportError("import ledger manifest is malformed") from exc
        if stored != manifest.as_dict():
            raise BusinessImportError("import ledger manifest differs from recomputed source")
        _verify_target_rows(conn, manifest)
        return stored
    tables = build_business_import_metadata().tables
    for name in IMPORT_TABLES + ("active_brief_slots",):
        if conn.execute(select(text("COUNT(*)")).select_from(tables[name])).scalar_one():
            raise BusinessImportError("target is not empty and has no matching import ledger")
    if conn.execute(select(import_ledger.c.source_id).limit(1)).first() is not None:
        raise BusinessImportError("target contains an unrelated prior import")
    return None


def _verify_target_rows(conn, manifest: ImportManifest) -> None:
    tables = build_business_import_metadata().tables
    for name in IMPORT_TABLES:
        table = tables[name]
        rows = [dict(row) for row in conn.execute(select(table)).mappings()]
        if name == "users":
            for row in rows:
                expected_digest = hashlib.sha256(row["username"].encode("utf-8")).digest()
                if row.get("username_digest") != expected_digest:
                    raise BusinessImportError("target verification mismatch for users.username_digest")
                row.pop("username_digest")
        primary_key = [column.name for column in table.primary_key.columns]
        rows.sort(key=lambda row: tuple(str(row[key]) for key in primary_key))
        digest = hashlib.sha256()
        for row in rows:
            digest.update(_canonical(row))
            digest.update(b"\n")
        if len(rows) != manifest.table_counts[name] or digest.hexdigest() != manifest.table_hashes[name]:
            raise BusinessImportError(f"target verification mismatch for {name}")
    expected_slots = {
        (row["conversation_id"], row["id"])
        for row in conn.execute(select(tables["planning_briefs"])).mappings()
        if row["status"] in {"collecting", "ready"}
    }
    actual_slots = {
        (row["conversation_id"], row["brief_id"])
        for row in conn.execute(select(tables["active_brief_slots"])).mappings()
    }
    if expected_slots != actual_slots:
        raise BusinessImportError("target verification mismatch for active brief slots")


def preview_business_import(source_path: str | Path, target_database) -> ImportManifest:
    """Validate source and empty/matching target without writing either database."""
    _assert_mysql_schema(target_database)
    path = Path(source_path).resolve()
    source = _readonly_source(path)
    try:
        rows, counts, hashes, omitted, omitted_presence = _source_inventory(source)
        manifest = _manifest(path, counts, hashes, omitted, omitted_presence)
        with target_database.transaction() as conn:
            previous = _target_ledger(conn, manifest)
        return ImportManifest(**{**manifest.__dict__, "already_applied": previous is not None})
    finally:
        source.rollback()
        source.close()


def _topological(rows: list[dict], key: str, dependency: str, table: str) -> list[dict]:
    by_id = {row[key]: row for row in rows}
    visiting: set[str] = set()
    complete: set[str] = set()
    ordered: list[dict] = []

    def visit(item_id):
        if item_id in complete:
            return
        if item_id in visiting:
            raise BusinessImportError(f"cyclic {table}.{dependency} references")
        visiting.add(item_id)
        parent = by_id.get(by_id[item_id].get(dependency))
        if parent is not None:
            visit(parent[key])
        visiting.remove(item_id)
        complete.add(item_id)
        ordered.append(by_id[item_id])

    for item_id in sorted(by_id, key=str):
        visit(item_id)
    return ordered


def _insert_source_rows(conn, source, table, name: str) -> None:
    metadata_table = table
    batch: list[dict] = []
    for row in _table_rows(source, name):
        if name == "users":
            row["username_digest"] = hashlib.sha256(row["username"].encode("utf-8")).digest()
        batch.append(row)
        if len(batch) == 250:
            conn.execute(insert(metadata_table), batch)
            batch.clear()
    if batch:
        conn.execute(insert(metadata_table), batch)


def apply_business_import(
    source_path: str | Path,
    target_database,
    *,
    expected_source_digest: str,
    fail_after_table: str | None = None,
) -> ImportManifest:
    """Atomically import one frozen source snapshot; safe reruns return no-op.

    ``fail_after_table`` exists only to exercise rollback behavior in isolated
    tests and is not exposed by the operator CLI.
    """
    _assert_mysql_schema(target_database)
    path = Path(source_path).resolve()
    source = _readonly_source(path)
    try:
        rows, counts, hashes, omitted, omitted_presence = _source_inventory(source)
        manifest = _manifest(path, counts, hashes, omitted, omitted_presence)
        if manifest.source_digest != expected_source_digest:
            raise BusinessImportError("source changed since preview; generate a new manifest")
        metadata = build_business_import_metadata()
        with _import_lock(target_database) as conn:
            with conn.begin():
                _validate_mysql_schema(conn)
                previous = _target_ledger(conn, manifest)
                if previous is not None:
                    return ImportManifest(**{**manifest.__dict__, "already_applied": True})
                order = (
                    "users", "user_profiles", "conversations", "itineraries",
                    "memory_facts", "user_memory_states", "conversation_memory_states",
                    "memory_extraction_jobs", "runs", "messages", "planning_briefs",
                    "run_events", "run_interaction_responses", "itinerary_enrichments",
                    "optimizer_shadow_runs",
                )
                for name in order:
                    values = rows[name]
                    if name == "memory_facts":
                        values = _topological(values, "id", "supersedes_id", name)
                    elif name == "runs":
                        values = _topological(values, "id", "retry_of_run_id", name)
                    table = metadata.tables[name]
                    if values:
                        if name == "users":
                            prepared = [dict(row, username_digest=hashlib.sha256(row["username"].encode("utf-8")).digest()) for row in values]
                        else:
                            prepared = values
                        for start in range(0, len(prepared), 250):
                            conn.execute(insert(table), prepared[start:start + 250])
                    if name == "planning_briefs":
                        slots = metadata.tables["active_brief_slots"]
                        active = [
                            {"conversation_id": row["conversation_id"], "brief_id": row["id"]}
                            for row in values if row["status"] in {"collecting", "ready"}
                        ]
                        if active:
                            conn.execute(insert(slots), active)
                    if fail_after_table == name:
                        raise RuntimeError("injected import failure after " + name)
                _verify_target_rows(conn, manifest)
                conn.execute(insert(import_ledger).values(
                    source_id=manifest.source_id,
                    source_digest=manifest.source_digest,
                    manifest_json=json.dumps(manifest.as_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    applied_at=datetime.now(timezone.utc).isoformat(),
                ))
        return manifest
    finally:
        source.rollback()
        source.close()


def verify_business_import(source_path: str | Path, target_database) -> ImportManifest:
    """Recompute source and target summaries and require a matching ledger."""
    manifest = preview_business_import(source_path, target_database)
    if not manifest.already_applied:
        raise BusinessImportError("no completed import ledger entry for this source")
    return manifest


def rollback_business_import(source_path: str | Path, target_database, *, expected_source_digest: str) -> ImportManifest:
    """Remove a verified import from its still-otherwise-empty target.

    Refuses if the source changed, any target row diverged, or unrelated target
    rows exist. The source and installed schema are retained.
    """
    _assert_mysql_schema(target_database)
    path = Path(source_path).resolve()
    source = _readonly_source(path)
    try:
        _, counts, hashes, omitted, omitted_presence = _source_inventory(source)
        manifest = _manifest(path, counts, hashes, omitted, omitted_presence)
        if manifest.source_digest != expected_source_digest:
            raise BusinessImportError("source changed since preview; rollback refused")
        tables = build_business_import_metadata().tables
        with _import_lock(target_database) as conn:
            with conn.begin():
                _validate_mysql_schema(conn)
                previous = _target_ledger(conn, manifest)
                if previous is None:
                    raise BusinessImportError("no matching completed import; rollback refused")
                # Null the only self-referencing imported FK before reverse-order deletion.
                from sqlalchemy import update
                conn.execute(update(tables["memory_facts"]).values(supersedes_id=None))
                reverse_order = (
                    "active_brief_slots", "optimizer_shadow_runs", "itinerary_enrichments",
                    "run_interaction_responses", "run_events", "messages", "planning_briefs",
                    "memory_extraction_jobs", "conversation_memory_states", "memory_facts",
                    "user_memory_states", "user_profiles", "runs", "itineraries",
                    "conversations", "users",
                )
                for name in reverse_order:
                    conn.execute(tables[name].delete())
                conn.execute(import_ledger.delete().where(import_ledger.c.source_id == manifest.source_id))
        return manifest
    finally:
        source.rollback()
        source.close()


def _write_manifest(path: str | Path, manifest: ImportManifest) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(manifest.as_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(target)


def _read_manifest(path: str | Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    required = {"source_id", "source_digest", "table_counts", "table_hashes", "omitted_table_counts", "omitted_table_presence"}
    if not isinstance(data, dict) or not required <= data.keys():
        raise BusinessImportError("manifest is incomplete")
    return data


def _compare_manifest(saved: dict, current: ImportManifest) -> None:
    """Compare every source-integrity field, excluding mutable run status."""
    recomputed = current.as_dict()
    for key in ("source_id", "source_digest", "table_counts", "table_hashes", "omitted_table_counts", "omitted_table_presence"):
        if recomputed[key] != saved.get(key):
            raise BusinessImportError("saved manifest does not match the recomputed preview: " + key)


def main(argv: list[str] | None = None) -> int:
    """Operator CLI; DSN is read from an environment variable, never persisted."""
    parser = argparse.ArgumentParser(description="Safely import SQLite business history into a staged MySQL database")
    commands = parser.add_subparsers(dest="command", required=True)
    initialize = commands.add_parser("init-schema", help="initialize or validate the empty staged MySQL schema only")
    initialize.add_argument("--dsn-env", default="WAYGENT_MYSQL_DSN")
    for name in ("preview", "apply", "verify", "rollback"):
        command = commands.add_parser(name)
        command.add_argument("--source", required=True, help="offline SQLite backup path")
        command.add_argument("--manifest", required=True, help="sanitized JSON manifest path")
        command.add_argument("--dsn-env", default="WAYGENT_MYSQL_DSN")
    args = parser.parse_args(argv)
    dsn = os.environ.get(args.dsn_env)
    if not dsn:
        parser.error(f"environment variable {args.dsn_env} is not set")
    from app.core.core_storage import CoreDatabase
    database = CoreDatabase(dsn)
    try:
        if args.command == "init-schema":
            from app.core.schema_management import upgrade_mysql_schema
            _assert_empty_schema_target(database)
            upgrade_mysql_schema(database)
            print("MySQL schema is initialized/validated; no business rows were imported.")
            return 0
        if args.command == "preview":
            manifest = preview_business_import(args.source, database)
            _write_manifest(args.manifest, manifest)
            print(f"Preview validated: {sum(manifest.table_counts.values())} rows across {len(manifest.table_counts)} business tables.")
            return 0
        saved = _read_manifest(args.manifest)
        if args.command == "apply":
            current = preview_business_import(args.source, database)
            _compare_manifest(saved, current)
            result = apply_business_import(args.source, database, expected_source_digest=saved["source_digest"])
            _write_manifest(args.manifest, result)
            print("Import verified and committed." if not result.already_applied else "This exact source was already imported; no rows were written.")
            return 0
        if args.command == "rollback":
            if verified_manifest := _read_manifest(args.manifest):
                current = preview_business_import(args.source, database)
                _compare_manifest(verified_manifest, current)
                rollback_business_import(args.source, database, expected_source_digest=verified_manifest["source_digest"])
                print("Verified import rows were removed; source backup and schema were preserved.")
                return 0
        verified = verify_business_import(args.source, database)
        _compare_manifest(saved, verified)
        _write_manifest(args.manifest, verified)
        print("Source and MySQL rows match the saved manifest.")
        return 0
    except (BusinessImportError, OSError, ValueError, RuntimeError) as exc:
        print(f"business-history import stopped: {exc}", file=sys.stderr)
        return 2
    finally:
        database.close()


if __name__ == "__main__":
    raise SystemExit(main())

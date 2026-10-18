"""Explicit, synchronous SQLAlchemy Core lifecycle; never selected implicitly."""
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path
from sqlalchemy import create_engine, event
from sqlalchemy.engine import URL, Connection

class CoreDatabase:
    def __init__(self, url: str | URL, **engine_options) -> None:
        self.engine = create_engine(url, pool_pre_ping=True, hide_parameters=True, **engine_options)
        self._closed = False
        if self.engine.dialect.name == "sqlite":
            @event.listens_for(self.engine, "connect")
            def configure(connection, _):
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA busy_timeout=30000")
                # Driver autocommit + explicit BEGIN: DDL and reads also belong
                # to the outer transaction (including Alembic version writes).
                connection.isolation_level = None
            @event.listens_for(self.engine, "begin")
            def begin(connection):
                connection.exec_driver_sql("BEGIN")

    @classmethod
    def sqlite(cls, path: str | Path, **options):
        return cls(URL.create("sqlite", database=str(Path(path).resolve())), **options)

    @contextmanager
    def connection(self) -> Iterator[Connection]:
        """Borrow a connection for explicit lifecycle/DDL, without auto-commit."""
        if self._closed:
            raise RuntimeError("CoreDatabase is closed")
        with self.engine.connect() as connection:
            yield connection

    @contextmanager
    def transaction(self) -> Iterator[Connection]:
        if self._closed:
            raise RuntimeError("CoreDatabase is closed")
        with self.engine.begin() as connection:
            yield connection

    def close(self) -> None:
        self._closed = True
        self.engine.dispose()

    def __enter__(self):
        if self._closed:
            raise RuntimeError("CoreDatabase is closed")
        return self

    def __exit__(self, *exc):
        self.close()

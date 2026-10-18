"""Explicit itinerary storage selection for request and worker seams.

SQLite remains the default until the complete cutover. Tests and transition
runtime configuration may bind a CoreDatabase without changing public route
or repository contracts.
"""

from contextlib import contextmanager

from app.core.database import get_conn


@contextmanager
def _bounded_revision_lock_wait(conn):
    """Cap backend lock waits, restoring pooled session settings on exit."""
    from app.planning.revision_timeout import remaining_deadline_seconds

    remaining = remaining_deadline_seconds()
    if remaining is None:
        yield
        return
    if remaining <= 0:
        raise TimeoutError("revision deadline expired before itinerary persistence")
    dialect = getattr(getattr(conn, "dialect", None), "name", None)
    if dialect == "sqlite" or dialect is None:
        if dialect == "sqlite":
            previous = int(conn.exec_driver_sql("PRAGMA busy_timeout").scalar_one())
        else:
            previous = int(conn.execute("PRAGMA busy_timeout").fetchone()[0])
        milliseconds = max(1, int(remaining * 1000))
        if dialect == "sqlite":
            conn.exec_driver_sql(f"PRAGMA busy_timeout={milliseconds}")
        else:
            conn.execute(f"PRAGMA busy_timeout={milliseconds}")
        try:
            yield
        finally:
            if dialect == "sqlite":
                conn.exec_driver_sql(f"PRAGMA busy_timeout={previous}")
            else:
                conn.execute(f"PRAGMA busy_timeout={previous}")
    elif dialect == "mysql":
        previous = int(conn.exec_driver_sql("SELECT @@SESSION.innodb_lock_wait_timeout").scalar_one())
        seconds = int(remaining)
        if seconds < 1:
            raise TimeoutError("less than one second remains for MySQL itinerary persistence")
        conn.exec_driver_sql(f"SET SESSION innodb_lock_wait_timeout={seconds}")
        try:
            yield
        finally:
            conn.exec_driver_sql(f"SET SESSION innodb_lock_wait_timeout={previous}")
    else:
        yield

_database = None


def configure_itinerary_backend(database=None) -> None:
    global _database
    _database = database


def itinerary_database():
    return _database


@contextmanager
def itinerary_connection(db_path=None):
    # An explicit CoreDatabase is an intentional backend selection. A plain
    # path is also explicit and must retain the legacy SQLite behavior, even
    # when a transition backend has been configured globally.
    database = db_path if getattr(db_path, "transaction", None) else None
    if db_path is None:
        database = _database
    if database is not None:
        with database.transaction() as conn:
            with _bounded_revision_lock_wait(conn):
                yield conn
        return
    with get_conn(db_path) as conn:
        with _bounded_revision_lock_wait(conn):
            yield conn

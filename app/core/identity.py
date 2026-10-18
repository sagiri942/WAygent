"""Legacy account/profile repositories and shared storage-independent errors."""
import sqlite3
from app.core.database import get_conn
from app.core.travel_memory import MemoryRepository

class DuplicateUsername(ValueError):
    pass

class UsernameDigestCollision(RuntimeError):
    pass

class UnsupportedProfileOperation(RuntimeError):
    pass

class SQLiteAccounts:
    def create(self, user_id, username, password_hash, created_at):
        try:
            with get_conn() as conn:
                conn.execute("INSERT INTO users (id,username,password_hash,created_at) VALUES (?,?,?,?)",
                             (user_id, username, password_hash, created_at))
        except sqlite3.IntegrityError as exc:
            if exc.sqlite_errorcode == sqlite3.SQLITE_CONSTRAINT_UNIQUE and self.by_username(username):
                raise DuplicateUsername("username exists") from exc
            raise

    def by_username(self, username):
        with get_conn() as conn:
            row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
            return dict(row) if row else None

    def get(self, user_id):
        with get_conn() as conn:
            row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            return dict(row) if row else None

    def update_password(self, user_id, password_hash):
        with get_conn() as conn:
            return conn.execute("UPDATE users SET password_hash=? WHERE id=?", (password_hash, user_id)).rowcount == 1

class SQLiteProfiles:
    def __init__(self, memories=None):
        self.memories = memories or MemoryRepository()

    def read(self, user_id):
        rows = self.memories.list(user_id, statuses={"active", "candidate"})
        with get_conn() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM itineraries WHERE user_id=?", (user_id,)).fetchone()
        return {"revision": self.memories.revision(user_id),
            "active_facts": [r for r in rows if r["status"] == "active"],
            "candidate_facts": [r for r in rows if r["status"] == "candidate"],
            "trip_count": int(row["n"] if row else 0)}
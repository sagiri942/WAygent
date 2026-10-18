"""Explicit Core account and manual profile adapters. Never select SQLite."""
import hashlib
import uuid
from sqlalchemy import select, insert, update, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.dialects.mysql import insert as mysql_insert
from app.core.identity_schema import users, memory_facts as facts, user_memory_states as states, itineraries
from app.core.travel_memory import (utcnow, decode_fact, MemoryNotFound,
    FACT_CATEGORIES, POLARITIES, FACT_STATUSES, SOURCE_KINDS,
    canonical_scope_key, normalize_value, memory_fingerprint, is_prohibited_memory_value)

from app.core.identity import DuplicateUsername, UsernameDigestCollision, UnsupportedProfileOperation

class CoreAccounts:
    def __init__(self, database, *, username_digest=None):
        self.database = database
        self.username_digest = username_digest or (lambda value: hashlib.sha256(value.encode("utf8")).digest())

    def by_username(self, username):
        with self.database.transaction() as conn:
            row = conn.execute(select(users).where(users.c.username_digest == self.username_digest(username))).mappings().first()
            if row and row["username"] != username:
                raise UsernameDigestCollision("username digest collision")
            return dict(row) if row else None

    def get(self, user_id):
        with self.database.transaction() as conn:
            row = conn.execute(select(users).where(users.c.id == user_id)).mappings().first()
            return dict(row) if row else None

    def create(self, user_id, username, password_hash, created_at):
        try:
            with self.database.transaction() as conn:
                conn.execute(insert(users).values(id=user_id, username=username,
                    username_digest=self.username_digest(username), password_hash=password_hash, created_at=created_at))
        except IntegrityError as exc:
            if exc.orig.args[0] != 1062:
                raise
            existing = self.by_username(username)
            if not existing:
                raise
            raise DuplicateUsername("username exists") from exc
        return self.get(user_id)

    def update_password(self, user_id, password_hash):
        with self.database.transaction() as conn:
            result = conn.execute(update(users).where(users.c.id == user_id).values(password_hash=password_hash))
            return result.rowcount == 1

class CoreProfileMemories:
    """Manual create/edit/read only; conversation lifecycle belongs to Ticket04."""
    def __init__(self, database):
        self.database = database

    def _revision(self, conn, user_id, *, lock=False):
        stmt = mysql_insert(states).values(user_id=user_id, revision=0, updated_at=utcnow())
        conn.execute(stmt.on_duplicate_key_update(user_id=stmt.inserted.user_id))
        query = select(states.c.revision).where(states.c.user_id == user_id)
        if lock:
            query = query.with_for_update()
        return int(conn.execute(query).scalar_one())

    def _bump(self, conn, user_id):
        conn.execute(update(states).where(states.c.user_id == user_id).values(revision=states.c.revision + 1, updated_at=utcnow()))

    def revision(self, user_id):
        with self.database.transaction() as conn:
            return self._revision(conn, user_id)

    def list(self, user_id, *, statuses=None):
        with self.database.transaction() as conn:
            return self._list(conn, user_id, statuses)

    def profile_snapshot(self, user_id, conn):
        """Read profile facts and revision in the caller's transaction."""
        revision = self._revision(conn, user_id, lock=True)
        return revision, self._list(conn, user_id)

    def _list(self, conn, user_id, statuses=None):
        chosen = statuses or {"active", "candidate"}
        if not chosen <= FACT_STATUSES:
            raise ValueError("invalid memory status")
        query = select(facts).where(facts.c.user_id == user_id, facts.c.status.in_(sorted(chosen))).order_by(facts.c.category, facts.c.updated_at.desc(), facts.c.id)
        return [decode_fact(row) for row in conn.execute(query).mappings()]

    def get(self, user_id, fact_id, conn=None):
        if conn is None:
            with self.database.transaction() as owned:
                return self.get(user_id, fact_id, owned)
        row = conn.execute(select(facts).where(facts.c.id == fact_id, facts.c.user_id == user_id)).mappings().first()
        if row is None:
            raise MemoryNotFound("memory fact not found")
        return decode_fact(row)

    def create(self, user_id, *, category, value_text, polarity="fact", scope_type="global", scope_key=None,
               status="active", source_kind="manual", sensitivity="normal", supersedes_id=None, conn=None):
        if conn is None:
            with self.database.transaction() as owned:
                self._revision(owned, user_id, lock=True)
                return self.create(user_id, category=category, value_text=value_text, polarity=polarity,
                    scope_type=scope_type, scope_key=scope_key, status=status, source_kind=source_kind,
                    sensitivity=sensitivity, supersedes_id=supersedes_id, conn=owned)
        self._revision(conn, user_id, lock=True)
        if source_kind != "manual" or status != "active":
            raise UnsupportedProfileOperation("Core non-manual memory lifecycle awaits Ticket04")
        if category not in FACT_CATEGORIES or polarity not in POLARITIES:
            raise ValueError("invalid memory fact")
        if status not in FACT_STATUSES or source_kind not in SOURCE_KINDS:
            raise ValueError("invalid memory metadata")
        if sensitivity not in {"normal", "protected"}:
            raise ValueError("invalid memory sensitivity")
        text = str(value_text).strip()
        normalized = normalize_value(text)
        if not normalized or len(text) > 500:
            raise ValueError("memory value must contain 1-500 characters")
        if is_prohibited_memory_value(text):
            raise ValueError("prohibited personal data cannot be stored as memory")
        scope_json = canonical_scope_key(scope_type, scope_key)
        fingerprint = memory_fingerprint(category, normalized, polarity, scope_type, scope_json)
        old = conn.execute(select(facts).where(facts.c.user_id == user_id, facts.c.fingerprint == fingerprint)).mappings().first()
        now = utcnow()
        if old:
            if old["status"] in {"deleted", "superseded"} and source_kind != "manual":
                return self.get(user_id, old["id"], conn)
            target = "active" if status == "active" or old["status"] == "active" else status
            conn.execute(update(facts).where(facts.c.id == old["id"]).values(status=target,
                source_kind=source_kind, sensitivity=sensitivity, evidence_sequences_json="[]",
                confidence=func.greatest(facts.c.confidence, 1.0), deleted_at=None, updated_at=now))
            if target == "active" and old["status"] != "active":
                self._bump(conn, user_id)
            return self.get(user_id, old["id"], conn)
        fact_id = str(uuid.uuid4())
        conn.execute(insert(facts).values(id=fact_id, user_id=user_id, category=category,
            value_text=text, normalized_value=normalized, polarity=polarity, scope_type=scope_type,
            scope_key=scope_json, status=status, source_kind=source_kind, sensitivity=sensitivity,
            evidence_sequences_json="[]", confidence=1.0, supersedes_id=supersedes_id,
            fingerprint=fingerprint, created_at=now, updated_at=now))
        if status == "active":
            self._bump(conn, user_id)
        return self.get(user_id, fact_id, conn)

    def replace(self, user_id, fact_id, **changes):
        with self.database.transaction() as conn:
            self._revision(conn, user_id, lock=True)
            old = self.get(user_id, fact_id, conn)
            if old["status"] not in {"active", "candidate"}:
                raise ValueError("memory fact cannot be edited")
            conn.execute(update(facts).where(facts.c.id == fact_id).values(status="superseded", updated_at=utcnow()))
            if old["status"] == "active":
                self._bump(conn, user_id)
            return self.create(user_id, **{key: changes.get(key, old[key]) for key in
                ("category", "value_text", "polarity", "scope_type", "scope_key")},
                status="active", source_kind="manual", sensitivity=old["sensitivity"], supersedes_id=fact_id, conn=conn)

    def approve(self, *args, **kw):
        raise UnsupportedProfileOperation("Memory approval is unavailable in this validation environment")

    def delete(self, *args, **kw):
        raise UnsupportedProfileOperation("Memory deletion is unavailable in this validation environment")

class CoreProfiles:
    def __init__(self, database, memories=None):
        self.database = database
        self.memories = memories or CoreProfileMemories(database)

    def read(self, user_id):
        with self.database.transaction() as conn:
            # Lock the same user revision used by manual writes, so one response
            # cannot mix a revision with facts from a different transaction.
            revision, rows = self.memories.profile_snapshot(user_id, conn)
            count = conn.execute(select(func.count()).select_from(itineraries).where(itineraries.c.user_id == user_id)).scalar_one()
            return {"revision": revision, "active_facts": [r for r in rows if r["status"] == "active"],
                    "candidate_facts": [r for r in rows if r["status"] == "candidate"], "trip_count": int(count)}
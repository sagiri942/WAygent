"""SQLAlchemy Core adapters for the existing travel-memory lifecycle.

These adapters are explicit and never selected by the default SQLite Runtime.
Conversation messages, Run transactions, and production cutover remain later
storage stages; this module owns the memory facts, frozen snapshots, summaries,
archive state, and extraction-job contracts.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import insert, select, update, func
from sqlalchemy.dialects.mysql import insert as mysql_insert

from app.core.identity_schema import (
    conversations, memory_facts as fact_table,
    user_memory_states as user_states,
    conversation_memory_states as conversation_states,
    memory_extraction_jobs as jobs_table, messages,
)
from app.core.travel_memory import (
    FACT_CATEGORIES, FACT_STATUSES, POLARITIES, SOURCE_KINDS,
    ArchivedConversationError, MemoryNotFound, canonical_scope_key,
    decode_fact, decode_memory_state, is_prohibited_memory_value,
    memory_fingerprint, normalize_value, utcnow,
)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class CoreMemoryRepository:
    """Owner-scoped facts with the SQLite repository's lifecycle semantics."""

    def __init__(self, database):
        self.database = database

    @staticmethod
    def _ensure_revision(conn, user_id: str, *, lock: bool = False) -> int:
        statement = mysql_insert(user_states).values(
            user_id=user_id, revision=0, updated_at=utcnow()
        )
        conn.execute(statement.on_duplicate_key_update(user_id=statement.inserted.user_id))
        query = select(user_states.c.revision).where(user_states.c.user_id == user_id)
        if lock:
            query = query.with_for_update()
        return int(conn.execute(query).scalar_one())

    @staticmethod
    def _bump_revision(conn, user_id: str) -> int:
        CoreMemoryRepository._ensure_revision(conn, user_id)
        conn.execute(update(user_states).where(user_states.c.user_id == user_id).values(
            revision=user_states.c.revision + 1, updated_at=utcnow()
        ))
        return int(conn.execute(select(user_states.c.revision).where(
            user_states.c.user_id == user_id
        )).scalar_one())

    def revision(self, user_id: str) -> int:
        with self.database.transaction() as conn:
            return self._ensure_revision(conn, user_id)

    def list(self, user_id: str, *, statuses: set[str] | None = None, conn=None) -> list[dict[str, Any]]:
        if conn is None:
            with self.database.transaction() as owned:
                return self.list(user_id, statuses=statuses, conn=owned)
        chosen = statuses or {"active", "candidate"}
        if not chosen <= FACT_STATUSES:
            raise ValueError("invalid memory status")
        query = select(fact_table).where(
            fact_table.c.user_id == user_id,
            fact_table.c.status.in_(sorted(chosen)),
        ).order_by(fact_table.c.category, fact_table.c.updated_at.desc(), fact_table.c.id)
        return [decode_fact(row) for row in conn.execute(query).mappings()]

    def profile_snapshot(self, user_id: str, conn) -> tuple[int, list[dict[str, Any]]]:
        """Read active/candidate facts under the caller's revision lock."""
        revision = self._ensure_revision(conn, user_id, lock=True)
        return revision, self.list(user_id, conn=conn)

    def snapshot(self, user_id: str, conn=None) -> tuple[int, list[dict[str, Any]]]:
        if conn is None:
            with self.database.transaction() as owned:
                return self.snapshot(user_id, owned)
        revision = self._ensure_revision(conn, user_id, lock=True)
        rows = conn.execute(select(fact_table).where(
            fact_table.c.user_id == user_id, fact_table.c.status == "active"
        ).order_by(fact_table.c.category, fact_table.c.updated_at.desc(), fact_table.c.id)).mappings()
        return revision, [decode_fact(row) for row in rows]

    def get(self, user_id: str, fact_id: str, conn=None) -> dict[str, Any]:
        if conn is None:
            with self.database.transaction() as owned:
                return self.get(user_id, fact_id, owned)
        row = conn.execute(select(fact_table).where(
            fact_table.c.user_id == user_id, fact_table.c.id == fact_id
        )).mappings().first()
        if row is None:
            raise MemoryNotFound("memory fact not found")
        return decode_fact(row)

    def create(
        self, user_id: str, *, category: str, value_text: str, polarity: str = "fact",
        scope_type: str = "global", scope_key: Any = None, status: str = "active",
        source_kind: str = "manual", sensitivity: str = "normal",
        source_conversation_id: str | None = None,
        evidence_sequences: list[int] | None = None, confidence: float = 1.0,
        supersedes_id: str | None = None, conn=None,
    ) -> dict[str, Any]:
        if conn is None:
            with self.database.transaction() as owned:
                self._ensure_revision(owned, user_id, lock=True)
                return self.create(user_id, category=category, value_text=value_text,
                    polarity=polarity, scope_type=scope_type, scope_key=scope_key,
                    status=status, source_kind=source_kind, sensitivity=sensitivity,
                    source_conversation_id=source_conversation_id,
                    evidence_sequences=evidence_sequences, confidence=confidence,
                    supersedes_id=supersedes_id, conn=owned)
        self._ensure_revision(conn, user_id, lock=True)
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
        if source_conversation_id:
            owned_conversation = conn.execute(select(conversations.c.id).where(
                conversations.c.id == source_conversation_id,
                conversations.c.user_id == user_id,
            )).scalar_one_or_none()
            if owned_conversation is None:
                raise MemoryNotFound("conversation not found")
        if supersedes_id:
            self.get(user_id, supersedes_id, conn)
        old = conn.execute(select(fact_table).where(
            fact_table.c.user_id == user_id, fact_table.c.fingerprint == fingerprint
        )).mappings().first()
        now = utcnow()
        evidence_json = _json(sorted({int(value) for value in (evidence_sequences or [])}))
        if old:
            old_status = old["status"]
            if old_status in {"deleted", "superseded"} and source_kind != "manual":
                return self.get(user_id, old["id"], conn)
            target_status = "active" if status == "active" or old_status == "active" else status
            conn.execute(update(fact_table).where(fact_table.c.id == old["id"]).values(
                status=target_status, source_kind=source_kind, sensitivity=sensitivity,
                source_conversation_id=func.coalesce(source_conversation_id, fact_table.c.source_conversation_id),
                evidence_sequences_json=evidence_json,
                confidence=func.greatest(fact_table.c.confidence, float(confidence)),
                deleted_at=None, updated_at=now,
            ))
            if target_status == "active" and old_status != "active":
                self._bump_revision(conn, user_id)
            return self.get(user_id, old["id"], conn)
        fact_id = str(uuid.uuid4())
        conn.execute(insert(fact_table).values(
            id=fact_id, user_id=user_id, category=category, value_text=text,
            normalized_value=normalized, polarity=polarity, scope_type=scope_type,
            scope_key=scope_json, status=status, source_kind=source_kind,
            sensitivity=sensitivity, source_conversation_id=source_conversation_id,
            evidence_sequences_json=evidence_json,
            confidence=max(0.0, min(float(confidence), 1.0)),
            supersedes_id=supersedes_id, fingerprint=fingerprint,
            created_at=now, updated_at=now,
        ))
        if status == "active":
            self._bump_revision(conn, user_id)
        return self.get(user_id, fact_id, conn)

    def replace(self, user_id: str, fact_id: str, **changes: Any) -> dict[str, Any]:
        with self.database.transaction() as conn:
            self._ensure_revision(conn, user_id, lock=True)
            old = self.get(user_id, fact_id, conn)
            if old["status"] not in {"active", "candidate"}:
                raise ValueError("memory fact cannot be edited")
            conn.execute(update(fact_table).where(fact_table.c.user_id == user_id,
                fact_table.c.id == fact_id).values(status="superseded", updated_at=utcnow()))
            if old["status"] == "active":
                self._bump_revision(conn, user_id)
            return self.create(user_id,
                **{key: changes.get(key, old[key]) for key in
                   ("category", "value_text", "polarity", "scope_type", "scope_key")},
                status="active", source_kind="manual", sensitivity=old["sensitivity"],
                supersedes_id=fact_id, conn=conn)

    def approve(self, user_id: str, fact_id: str) -> dict[str, Any]:
        with self.database.transaction() as conn:
            self._ensure_revision(conn, user_id, lock=True)
            fact = self.get(user_id, fact_id, conn)
            if fact["status"] != "candidate":
                raise ValueError("only candidate memory can be approved")
            conn.execute(update(fact_table).where(fact_table.c.user_id == user_id,
                fact_table.c.id == fact_id).values(status="active", updated_at=utcnow()))
            self._bump_revision(conn, user_id)
            return self.get(user_id, fact_id, conn)

    def delete(self, user_id: str, fact_id: str) -> dict[str, Any]:
        with self.database.transaction() as conn:
            self._ensure_revision(conn, user_id, lock=True)
            fact = self.get(user_id, fact_id, conn)
            if fact["status"] == "deleted":
                return fact
            now = utcnow()
            conn.execute(update(fact_table).where(fact_table.c.user_id == user_id,
                fact_table.c.id == fact_id).values(status="deleted", deleted_at=now, updated_at=now))
            if fact["status"] == "active":
                self._bump_revision(conn, user_id)
            return self.get(user_id, fact_id, conn)

    def supersede(self, user_id: str, fact_id: str) -> dict[str, Any]:
        with self.database.transaction() as conn:
            self._ensure_revision(conn, user_id, lock=True)
            fact = self.get(user_id, fact_id, conn)
            if fact["status"] in {"superseded", "deleted"}:
                return fact
            conn.execute(update(fact_table).where(fact_table.c.user_id == user_id,
                fact_table.c.id == fact_id).values(status="superseded", updated_at=utcnow()))
            if fact["status"] == "active":
                self._bump_revision(conn, user_id)
            return self.get(user_id, fact_id, conn)

    @staticmethod
    def format_for_prompt(facts: list[dict[str, Any]]) -> str:
        from app.core.travel_memory import MemoryRepository
        return MemoryRepository.format_for_prompt(facts)


class CoreConversationMemoryRepository:
    def __init__(self, database, facts: CoreMemoryRepository | None = None):
        self.database = database
        self.facts = facts or CoreMemoryRepository(database)

    def ensure_snapshot(self, user_id: str, conversation_id: str, conn=None) -> dict[str, Any]:
        if conn is None:
            with self.database.transaction() as owned:
                return self.ensure_snapshot(user_id, conversation_id, owned)
        owner = conn.execute(select(conversations.c.status).where(
            conversations.c.id == conversation_id, conversations.c.user_id == user_id
        ).with_for_update()).scalar_one_or_none()
        if owner is None:
            raise MemoryNotFound("conversation not found")
        current = conn.execute(select(conversation_states).where(
            conversation_states.c.conversation_id == conversation_id,
            conversation_states.c.user_id == user_id,
        )).mappings().first()
        if current:
            return decode_memory_state(current)
        if owner == "archived":
            raise ArchivedConversationError("conversation_archived")
        revision, snapshot = self.facts.snapshot(user_id, conn)
        now = utcnow()
        conn.execute(insert(conversation_states).values(
            conversation_id=conversation_id, user_id=user_id,
            profile_revision=revision, profile_snapshot_json=_json(snapshot),
            created_at=now, updated_at=now,
        ))
        return self.get(user_id, conversation_id, conn)

    def get(self, user_id: str, conversation_id: str, conn=None) -> dict[str, Any]:
        if conn is None:
            with self.database.transaction() as owned:
                return self.get(user_id, conversation_id, owned)
        row = conn.execute(select(conversation_states).where(
            conversation_states.c.conversation_id == conversation_id,
            conversation_states.c.user_id == user_id,
        )).mappings().first()
        if row is None:
            raise MemoryNotFound("conversation memory not initialized")
        return decode_memory_state(row)

    def update_summary(self, user_id: str, conversation_id: str, summary: dict[str, Any],
                       through_sequence: int, estimated_tokens: int) -> dict[str, Any]:
        with self.database.transaction() as conn:
            now = utcnow()
            conn.execute(update(conversation_states).where(
                conversation_states.c.conversation_id == conversation_id,
                conversation_states.c.user_id == user_id,
                conversation_states.c.summarized_through_sequence < through_sequence,
            ).values(summary_json=_json(summary), summarized_through_sequence=through_sequence,
                summary_count=conversation_states.c.summary_count + 1,
                estimated_context_tokens=estimated_tokens, updated_at=now))
            return self.get(user_id, conversation_id, conn)

    def record_estimate(self, user_id: str, conversation_id: str, tokens: int) -> None:
        with self.database.transaction() as conn:
            conn.execute(update(conversation_states).where(
                conversation_states.c.conversation_id == conversation_id,
                conversation_states.c.user_id == user_id,
            ).values(estimated_context_tokens=tokens, updated_at=utcnow()))

    def archive(self, user_id: str, conversation_id: str,
                through_sequence: int | None = None, conn=None) -> dict[str, Any]:
        if conn is None:
            with self.database.transaction() as owned:
                return self.archive(user_id, conversation_id, through_sequence, owned)
        state = self.ensure_snapshot(user_id, conversation_id, conn)
        owner = conn.execute(select(conversations.c.status).where(
            conversations.c.id == conversation_id, conversations.c.user_id == user_id
        ).with_for_update()).scalar_one_or_none()
        if owner is None:
            raise MemoryNotFound("conversation not found")
        if owner == "archived":
            return state
        if through_sequence is None:
            through_sequence = int(conn.execute(select(func.coalesce(func.max(messages.c.sequence), 0)).where(
                messages.c.conversation_id == conversation_id,
                messages.c.user_id == user_id,
            )).scalar_one())
        now = utcnow()
        conn.execute(update(conversations).where(
            conversations.c.id == conversation_id, conversations.c.user_id == user_id
        ).values(status="archived", archived_at=now, updated_at=now))
        if through_sequence > 0:
            CoreMemoryJobRepository(self.database).enqueue(
                user_id, conversation_id, "archive", 1, through_sequence, conn
            )
            final_status, finalized_at = "pending", None
        else:
            final_status, finalized_at = "succeeded", now
        conn.execute(update(conversation_states).where(
            conversation_states.c.conversation_id == conversation_id,
            conversation_states.c.user_id == user_id,
        ).values(finalization_status=final_status, finalized_at=finalized_at,
            last_error_code=None, updated_at=now))
        return self.get(user_id, conversation_id, conn)


class CoreMemoryJobRepository:
    def __init__(self, database):
        self.database = database

    def enqueue(self, user_id: str, conversation_id: str, kind: str,
                from_sequence: int, through_sequence: int, conn=None) -> dict[str, Any] | None:
        if through_sequence < from_sequence or through_sequence <= 0:
            return None
        if kind not in {"pre_summary", "archive"}:
            raise ValueError("invalid memory job kind")
        if conn is None:
            with self.database.transaction() as owned:
                return self.enqueue(user_id, conversation_id, kind, from_sequence,
                    through_sequence, owned)
        owner = conn.execute(select(conversations.c.id).where(
            conversations.c.id == conversation_id, conversations.c.user_id == user_id
        )).scalar_one_or_none()
        if owner is None:
            raise MemoryNotFound("conversation not found")
        start = max(1, int(from_sequence))
        end = int(through_sequence)
        now, job_id = utcnow(), str(uuid.uuid4())
        statement = mysql_insert(jobs_table).values(
            id=job_id, conversation_id=conversation_id, user_id=user_id, kind=kind,
            from_sequence=start, through_sequence=end, status="pending",
            attempts=0, created_at=now, updated_at=now,
        )
        conn.execute(statement.on_duplicate_key_update(id=jobs_table.c.id))
        row = conn.execute(select(jobs_table).where(
            jobs_table.c.conversation_id == conversation_id,
            jobs_table.c.user_id == user_id, jobs_table.c.kind == kind,
            jobs_table.c.from_sequence == start, jobs_table.c.through_sequence == end,
        )).mappings().first()
        return dict(row) if row else None

    def reset_running(self) -> int:
        with self.database.transaction() as conn:
            result = conn.execute(update(jobs_table).where(
                jobs_table.c.status == "running"
            ).values(status="pending", updated_at=utcnow()))
            return result.rowcount

    def claim_next(self) -> dict[str, Any] | None:
        with self.database.transaction() as conn:
            now = utcnow()
            row = conn.execute(select(jobs_table).where(
                jobs_table.c.status == "pending",
                (jobs_table.c.next_attempt_at.is_(None)) | (jobs_table.c.next_attempt_at <= now),
            ).order_by(jobs_table.c.created_at, jobs_table.c.id).limit(1)
                .with_for_update(skip_locked=True)).mappings().first()
            if row is None:
                return None
            result = conn.execute(update(jobs_table).where(
                jobs_table.c.id == row["id"], jobs_table.c.status == "pending"
            ).values(status="running", attempts=jobs_table.c.attempts + 1, updated_at=now))
            if result.rowcount != 1:
                return None
            claimed = conn.execute(select(jobs_table).where(
                jobs_table.c.id == row["id"]
            )).mappings().one()
            return dict(claimed)

    def get(self, user_id: str, job_id: str) -> dict[str, Any]:
        with self.database.transaction() as conn:
            row = conn.execute(select(jobs_table).where(
                jobs_table.c.user_id == user_id, jobs_table.c.id == job_id
            )).mappings().first()
            if row is None:
                raise MemoryNotFound("memory job not found")
            return dict(row)

    def complete(self, job_id: str) -> None:
        with self.database.transaction() as conn:
            row = conn.execute(select(jobs_table.c.conversation_id, jobs_table.c.user_id,
                jobs_table.c.kind).where(jobs_table.c.id == job_id).with_for_update()).first()
            if row is None:
                return
            now = utcnow()
            conn.execute(update(jobs_table).where(jobs_table.c.id == job_id).values(
                status="succeeded", finished_at=now, last_error_code=None, updated_at=now
            ))
            if row.kind == "archive":
                conn.execute(update(conversation_states).where(
                    conversation_states.c.conversation_id == row.conversation_id,
                    conversation_states.c.user_id == row.user_id,
                ).values(finalization_status="succeeded", finalized_at=now,
                    last_error_code=None, updated_at=now))

    def fail(self, job_id: str, error_code: str) -> None:
        with self.database.transaction() as conn:
            row = conn.execute(select(jobs_table.c.attempts, jobs_table.c.conversation_id,
                jobs_table.c.user_id, jobs_table.c.kind).where(
                    jobs_table.c.id == job_id
                ).with_for_update()).first()
            if row is None:
                return
            attempts = int(row.attempts)
            terminal = attempts >= 5
            delay = min(2 ** max(0, attempts - 1) * 30, 7200)
            next_at = None if terminal else (
                datetime.now(timezone.utc) + timedelta(seconds=delay)
            ).isoformat()
            now = utcnow()
            conn.execute(update(jobs_table).where(jobs_table.c.id == job_id).values(
                status="failed" if terminal else "pending", next_attempt_at=next_at,
                last_error_code=error_code, updated_at=now,
            ))
            if terminal and row.kind == "archive":
                conn.execute(update(conversation_states).where(
                    conversation_states.c.conversation_id == row.conversation_id,
                    conversation_states.c.user_id == row.user_id,
                ).values(finalization_status="failed", last_error_code=error_code,
                    updated_at=now))

    def retry_archive(self, user_id: str, conversation_id: str) -> dict[str, Any]:
        with self.database.transaction() as conn:
            row = conn.execute(select(jobs_table).where(
                jobs_table.c.conversation_id == conversation_id,
                jobs_table.c.user_id == user_id, jobs_table.c.kind == "archive",
            ).order_by(jobs_table.c.created_at.desc(), jobs_table.c.id.desc())
                .limit(1).with_for_update()).mappings().first()
            if row is None:
                raise MemoryNotFound("archive memory job not found")
            if row["status"] != "failed":
                raise ValueError("archive memory job is not failed")
            now = utcnow()
            conn.execute(update(jobs_table).where(jobs_table.c.id == row["id"]).values(
                status="pending", attempts=0, next_attempt_at=None,
                last_error_code=None, updated_at=now,
            ))
            conn.execute(update(conversation_states).where(
                conversation_states.c.conversation_id == conversation_id,
                conversation_states.c.user_id == user_id,
            ).values(finalization_status="pending", last_error_code=None, updated_at=now))
            refreshed = conn.execute(select(jobs_table).where(
                jobs_table.c.user_id == user_id, jobs_table.c.id == row["id"]
            )).mappings().one()
            return dict(refreshed)

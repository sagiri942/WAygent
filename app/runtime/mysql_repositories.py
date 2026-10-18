"""SQLAlchemy Core Runtime adapters for the existing repository seams.

These adapters are selected only when a CoreDatabase is explicitly passed;
the application's SQLite default and production wiring remain unchanged.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, case, exists, func, insert, or_, select, update
from sqlalchemy.exc import IntegrityError

from app.core.identity_schema import (
    active_brief_slots, conversations, conversation_memory_states, messages, planning_briefs,
    run_events, run_interaction_responses, runs,
)
from app.core.planning_constraints import build_brief_projection, normalize_brief_data
from app.core.travel_memory import ArchivedConversationError
from app.runtime.models import (
    DisconnectPolicy, RunKind, RunStatus, TERMINAL_STATUSES,
    concurrency_key, validate_transition,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _loads(value: str | None, default: Any) -> Any:
    return json.loads(value) if value else default


def _mapping(row):
    return dict(row) if row is not None else None


def _decode_message(row):
    result = _mapping(row)
    from app.chat.artifacts import validate_message_artifacts
    result["artifacts"] = validate_message_artifacts(_loads(result.pop("artifacts_json", None), []))
    return result


def _decode_run(row):
    if row is None:
        from app.runtime.repositories import OwnedResourceNotFound
        raise OwnedResourceNotFound("run not found")
    result = _mapping(row)
    result["request_snapshot"] = _loads(result.pop("request_snapshot_json"), {})
    result["error_public"] = _loads(result.pop("error_public_json"), None)
    result.pop("active_conversation_id", None)
    return result


def _decode_brief(row):
    result = _mapping(row)
    result.pop("active_conversation_id", None)
    result["data"] = normalize_brief_data(_loads(result.pop("data_json"), {}))
    result["missing_fields"] = _loads(result.pop("missing_fields_json"), [])
    result["submission_snapshot"] = _loads(result.pop("submission_snapshot_json"), None)
    result["memory_projection"] = _loads(result.pop("memory_projection_json"), None)
    return result


class MySQLConversationRepository:
    def __init__(self, database):
        self.database = database

    @staticmethod
    def _decode(row):
        result = _mapping(row)
        result["has_active_planning"] = bool(result.get("has_active_planning", False))
        result["has_waiting_user"] = bool(result.get("has_waiting_user", False))
        result["has_ready_brief"] = bool(result.get("has_ready_brief", False))
        result["has_unread_completed"] = bool(result.get("has_unread_completed", False))
        return result

    @staticmethod
    def _conversation_query():
        active_planning = exists(select(1).where(
            runs.c.conversation_id == conversations.c.id,
            runs.c.user_id == conversations.c.user_id,
            runs.c.kind.in_(["chat", "travel_plan", "revision"]),
            runs.c.status.in_(["queued", "running"]),
        ))
        waiting_user = exists(select(1).where(
            runs.c.conversation_id == conversations.c.id,
            runs.c.user_id == conversations.c.user_id,
            runs.c.kind.in_(["chat", "travel_plan", "revision"]),
            runs.c.status == "waiting_user",
        ))
        ready_brief = exists(select(1).where(
            planning_briefs.c.conversation_id == conversations.c.id,
            planning_briefs.c.user_id == conversations.c.user_id,
            planning_briefs.c.status == "ready",
        ))
        unread_completed = exists(select(1).where(
            runs.c.conversation_id == conversations.c.id,
            runs.c.user_id == conversations.c.user_id,
            runs.c.kind.in_(["travel_plan", "revision"]),
            runs.c.status == "succeeded",
            func.coalesce(runs.c.finished_at, runs.c.updated_at, runs.c.created_at)
            > func.coalesce(conversations.c.last_viewed_at, conversations.c.created_at),
        ))
        memory_join = conversations.outerjoin(
            conversation_memory_states,
            and_(
                conversation_memory_states.c.conversation_id == conversations.c.id,
                conversation_memory_states.c.user_id == conversations.c.user_id,
            ),
        )
        active_status = conversations.c.status == "active"
        return select(
            conversations,
            func.coalesce(conversation_memory_states.c.finalization_status, "none").label("finalization_status"),
            conversation_memory_states.c.last_error_code.label("memory_error_code"),
            case((and_(active_status, active_planning), True), else_=False).label("has_active_planning"),
            case((and_(active_status, waiting_user), True), else_=False).label("has_waiting_user"),
            case((and_(active_status, ready_brief), True), else_=False).label("has_ready_brief"),
            case((and_(active_status, unread_completed), True), else_=False).label("has_unread_completed"),
        ).select_from(memory_join)

    def create(self, user_id: str, title: str = "") -> dict[str, Any]:
        conversation_id, now = str(uuid.uuid4()), _now()
        with self.database.transaction() as conn:
            conn.execute(insert(conversations).values(
                id=conversation_id, user_id=user_id, title=title.strip(), status="active",
                last_viewed_at=now, created_at=now, updated_at=now,
            ))
        return self.get(user_id, conversation_id)

    def get(self, user_id: str, conversation_id: str) -> dict[str, Any]:
        from app.runtime.repositories import OwnedResourceNotFound
        query = self._conversation_query().where(
            conversations.c.id == conversation_id, conversations.c.user_id == user_id,
        )
        with self.database.transaction() as conn:
            row = conn.execute(query).mappings().first()
        if not row:
            raise OwnedResourceNotFound("conversation not found")
        result = dict(row)
        return self._decode(result)

    def list(self, user_id: str, limit: int = 50, related_itinerary_id: str | None = None):
        query = self._conversation_query().where(conversations.c.user_id == user_id)
        if related_itinerary_id:
            query = query.where(exists(select(1).select_from(messages).where(
                messages.c.conversation_id == conversations.c.id,
                messages.c.user_id == conversations.c.user_id,
                messages.c.related_itinerary_id == related_itinerary_id,
            )) | exists(select(1).select_from(runs).where(
                runs.c.conversation_id == conversations.c.id,
                runs.c.user_id == conversations.c.user_id,
                runs.c.result_itinerary_id == related_itinerary_id,
            )))
        query = query.order_by(conversations.c.updated_at.desc(), conversations.c.id.desc()).limit(max(1, min(limit, 100)))
        with self.database.transaction() as conn:
            rows = conn.execute(query).mappings().all()
        return [self._decode(row) for row in rows]

    def mark_viewed(self, user_id: str, conversation_id: str):
        with self.database.transaction() as conn:
            result = conn.execute(update(conversations).where(
                conversations.c.id == conversation_id, conversations.c.user_id == user_id,
            ).values(last_viewed_at=_now()))
        if result.rowcount != 1:
            from app.runtime.repositories import OwnedResourceNotFound
            raise OwnedResourceNotFound("conversation not found")
        return self.get(user_id, conversation_id)

    def archive(self, user_id: str, conversation_id: str):
        from app.core.mysql_travel_memory import CoreConversationMemoryRepository
        from app.core.travel_memory import MemoryNotFound
        try:
            with self.database.transaction() as conn:
                CoreConversationMemoryRepository(self.database).archive(
                    user_id, conversation_id, conn=conn,
                )
        except MemoryNotFound as exc:
            from app.runtime.repositories import OwnedResourceNotFound
            raise OwnedResourceNotFound("conversation not found") from exc
        return self.get(user_id, conversation_id)

    def add_user_message_and_run(self, user_id: str, conversation_id: str, content: str,
                                 *, related_run_id=None, related_itinerary_id=None):
        from app.runtime.repositories import ConversationRunActive, OwnedResourceNotFound
        now, message_id, run_id = _now(), str(uuid.uuid4()), str(uuid.uuid4())
        try:
            with self.database.transaction() as conn:
                owner = conn.execute(select(conversations.c.title, conversations.c.status).where(
                    conversations.c.id == conversation_id, conversations.c.user_id == user_id,
                ).with_for_update()).mappings().first()
                if not owner:
                    raise OwnedResourceNotFound("conversation not found")
                if owner["status"] == "archived":
                    raise ArchivedConversationError("conversation_archived")
                active = conn.execute(select(runs).where(
                    runs.c.conversation_id == conversation_id, runs.c.user_id == user_id,
                    runs.c.kind.in_(["chat", "travel_plan", "revision"]),
                    runs.c.status.in_(["queued", "running", "waiting_user"]),
                ).order_by(runs.c.created_at.desc(), runs.c.id.desc()).limit(1)).mappings().first()
                if active:
                    raise ConversationRunActive(_decode_run(active))
                from app.core.mysql_travel_memory import CoreConversationMemoryRepository
                CoreConversationMemoryRepository(self.database).ensure_snapshot(user_id, conversation_id, conn)
                sequence = int(conn.execute(select(func.coalesce(func.max(messages.c.sequence), 0) + 1).where(
                    messages.c.conversation_id == conversation_id,
                )).scalar_one())
                conn.execute(insert(messages).values(
                    id=message_id, conversation_id=conversation_id, user_id=user_id, role="user",
                    content=content, artifacts_json="[]", sequence=sequence,
                    related_run_id=related_run_id, related_itinerary_id=related_itinerary_id, created_at=now,
                ))
                snapshot = {"message_id": message_id, "text": content,
                            "related_run_id": related_run_id, "related_itinerary_id": related_itinerary_id}
                run = MySQLRunRepository(self.database).insert(
                    conn, run_id=run_id, user_id=user_id, kind=RunKind.CHAT,
                    concurrency_key=concurrency_key(RunKind.CHAT, conversation_id=conversation_id),
                    request_snapshot=snapshot, conversation_id=conversation_id,
                )
                title = str(owner["title"] or "").strip()
                values = {"updated_at": now}
                if sequence == 1 and title in {"", "新的旅行对话"}:
                    values["title"] = content.strip()[:24]
                conn.execute(update(conversations).where(conversations.c.id == conversation_id).values(**values))
        except IntegrityError as exc:
            with self.database.transaction() as conn:
                active = conn.execute(select(runs).where(
                    runs.c.conversation_id == conversation_id, runs.c.user_id == user_id,
                    runs.c.kind.in_(["chat", "travel_plan", "revision"]),
                    runs.c.status.in_(["queued", "running", "waiting_user"]),
                ).limit(1)).mappings().first()
            if active:
                raise ConversationRunActive(_decode_run(active)) from exc
            raise
        return ({"id": message_id, "conversation_id": conversation_id, "user_id": user_id,
                 "role": "user", "content": content, "artifacts": [], "sequence": sequence,
                 "related_run_id": related_run_id, "related_itinerary_id": related_itinerary_id,
                 "created_at": now}, run)

    def add_message(self, user_id, conversation_id, role, content, *, related_run_id=None,
                    related_itinerary_id=None, artifacts=None):
        if role not in {"user", "assistant", "system"}:
            raise ValueError("invalid message role")
        from app.chat.artifacts import validate_message_artifacts
        from app.runtime.repositories import OwnedResourceNotFound
        safe = validate_message_artifacts(artifacts)
        message_id, now = str(uuid.uuid4()), _now()
        with self.database.transaction() as conn:
            owner = conn.execute(select(conversations.c.title, conversations.c.status).where(
                conversations.c.id == conversation_id, conversations.c.user_id == user_id,
            ).with_for_update()).mappings().first()
            if not owner:
                raise OwnedResourceNotFound("conversation not found")
            if owner["status"] == "archived":
                raise ArchivedConversationError("conversation_archived")
            from app.core.mysql_travel_memory import CoreConversationMemoryRepository
            CoreConversationMemoryRepository(self.database).ensure_snapshot(user_id, conversation_id, conn)
            sequence = int(conn.execute(select(func.coalesce(func.max(messages.c.sequence), 0) + 1).where(
                messages.c.conversation_id == conversation_id,
            )).scalar_one())
            conn.execute(insert(messages).values(id=message_id, conversation_id=conversation_id,
                user_id=user_id, role=role, content=content, artifacts_json=_json(safe), sequence=sequence,
                related_run_id=related_run_id, related_itinerary_id=related_itinerary_id, created_at=now))
            values = {"updated_at": now}
            if role == "user" and sequence == 1 and str(owner["title"] or "").strip() in {"", "新的旅行对话"}:
                values["title"] = content.strip()[:24]
            conn.execute(update(conversations).where(conversations.c.id == conversation_id).values(**values))
        return {"id": message_id, "conversation_id": conversation_id, "user_id": user_id,
                "role": role, "content": content, "artifacts": safe, "sequence": sequence,
                "related_run_id": related_run_id, "related_itinerary_id": related_itinerary_id,
                "created_at": now}

    def messages(self, user_id, conversation_id, *, after_sequence=0, limit=100):
        self.get(user_id, conversation_id)
        with self.database.transaction() as conn:
            rows = conn.execute(select(messages).where(
                messages.c.conversation_id == conversation_id,
                messages.c.sequence > max(0, after_sequence),
            ).order_by(messages.c.sequence).limit(max(1, min(limit, 200)))).mappings().all()
        return [_decode_message(row) for row in rows]

    def recent_messages(self, user_id, conversation_id, *, after_sequence=0, limit=200):
        self.get(user_id, conversation_id)
        query = select(messages).where(messages.c.conversation_id == conversation_id,
            messages.c.sequence > max(0, after_sequence)).order_by(messages.c.sequence.desc()).limit(max(1, min(limit, 500)))
        with self.database.transaction() as conn:
            rows = conn.execute(query).mappings().all()
        return [_decode_message(row) for row in reversed(rows)]

    def context_messages(self, user_id, conversation_id, *, after_sequence=0,
                         oldest_limit=240, recent_limit=15):
        self.get(user_id, conversation_id)
        oldest_query = select(messages).where(messages.c.conversation_id == conversation_id,
            messages.c.sequence > max(0, after_sequence)).order_by(messages.c.sequence).limit(max(1, min(oldest_limit, 500)))
        recent_query = select(messages).where(messages.c.conversation_id == conversation_id,
            messages.c.sequence > max(0, after_sequence)).order_by(messages.c.sequence.desc()).limit(max(1, min(recent_limit, 100)))
        with self.database.transaction() as conn:
            oldest = conn.execute(oldest_query).mappings().all()
            recent = conn.execute(recent_query).mappings().all()
        merged = {row["id"]: _decode_message(row) for row in (*oldest, *recent)}
        return sorted(merged.values(), key=lambda row: int(row["sequence"]))

    def message_range(self, user_id, conversation_id, from_sequence, through_sequence):
        self.get(user_id, conversation_id)
        with self.database.transaction() as conn:
            rows = conn.execute(select(messages).where(
                messages.c.conversation_id == conversation_id,
                messages.c.sequence.between(from_sequence, through_sequence),
            ).order_by(messages.c.sequence)).mappings().all()
        return [_decode_message(row) for row in rows]


class MySQLRunRepository:
    def __init__(self, database):
        self.database = database

    def insert(self, conn, *, run_id, user_id, kind, concurrency_key, request_snapshot,
               conversation_id=None, disconnect_policy=DisconnectPolicy.CONTINUE, retry_of_run_id=None):
        now = _now()
        conn.execute(insert(runs).values(
            id=run_id, user_id=user_id, conversation_id=conversation_id,
            kind=RunKind(kind).value, status=RunStatus.QUEUED.value,
            concurrency_key=concurrency_key, request_snapshot_json=_json(request_snapshot),
            disconnect_policy=DisconnectPolicy(disconnect_policy).value,
            retry_of_run_id=retry_of_run_id, created_at=now, queued_at=now, updated_at=now,
        ))
        return _decode_run(conn.execute(select(runs).where(runs.c.id == run_id)).mappings().one())

    def get(self, user_id, run_id):
        from app.runtime.repositories import OwnedResourceNotFound
        with self.database.transaction() as conn:
            row = conn.execute(select(runs).where(runs.c.id == run_id, runs.c.user_id == user_id)).mappings().first()
        if row is None:
            raise OwnedResourceNotFound("run not found")
        return _decode_run(row)

    def get_internal(self, run_id):
        with self.database.transaction() as conn:
            row = conn.execute(select(runs).where(runs.c.id == run_id)).mappings().first()
        return _decode_run(row)

    def list(self, user_id, *, conversation_id=None, active_only=False, limit=100):
        query = select(runs).where(runs.c.user_id == user_id)
        if conversation_id:
            query = query.where(runs.c.conversation_id == conversation_id)
        if active_only:
            query = query.where(runs.c.status.in_(["queued", "running", "waiting_user"]))
        query = query.order_by(runs.c.created_at.desc(), runs.c.id.desc()).limit(max(1, min(limit, 200)))
        with self.database.transaction() as conn:
            rows = conn.execute(query).mappings().all()
        return [_decode_run(row) for row in rows]

    def transition(self, run_id, target, *, expected=None, error_public=None, error_internal=None,
                   result_itinerary_id=None, outstanding_interaction_id=None):
        from app.runtime.repositories import OwnedResourceNotFound
        target_status = RunStatus(target)
        with self.database.transaction() as conn:
            row = conn.execute(select(runs).where(runs.c.id == run_id).with_for_update()).mappings().first()
            if row is None:
                raise OwnedResourceNotFound("run not found")
            current = RunStatus(row["status"])
            if expected and current not in {RunStatus(value) for value in expected}:
                raise ValueError(f"run status is {current.value}")
            validate_transition(current, target_status)
            now = _now()
            values = {
                "status": target_status.value,
                "started_at": now if target_status is RunStatus.RUNNING and not row["started_at"] else row["started_at"],
                "finished_at": now if target_status in TERMINAL_STATUSES else None,
                "updated_at": now,
                "error_public_json": _json(error_public) if error_public else None,
                "error_internal": error_internal,
                "result_itinerary_id": result_itinerary_id or row["result_itinerary_id"],
                "outstanding_interaction_id": outstanding_interaction_id if target_status is RunStatus.WAITING_USER else None,
            }
            conn.execute(update(runs).where(runs.c.id == run_id).values(**values))
            changed = conn.execute(select(runs).where(runs.c.id == run_id)).mappings().one()
            event_specs = [("custom", {"kind": "run.status", "status": target_status.value})]
            if target_status is RunStatus.FAILED and error_public:
                event_specs.append(("error", error_public))
            if target_status in TERMINAL_STATUSES:
                event_specs.append(("end", {"status": target_status.value}))
            persisted = self._append_events(conn, run_id, event_specs, now)
        result = _decode_run(changed)
        result["_durable_events"] = persisted
        return result

    @staticmethod
    def _append_events(conn, run_id, event_specs, now):
        events = []
        for kind, payload in event_specs:
            sequence = int(conn.execute(select(func.coalesce(func.max(run_events.c.sequence), 0) + 1).where(
                run_events.c.run_id == run_id,
            )).scalar_one())
            event = {"id": str(uuid.uuid4()), "run_id": run_id, "sequence": sequence,
                     "kind": kind, "payload": payload, "durable": True, "created_at": now}
            conn.execute(insert(run_events).values(
                id=event["id"], run_id=run_id, sequence=sequence, kind=kind,
                payload_json=_json(payload), durable=1, created_at=now,
            ))
            events.append(event)
        return events

    def accept_interaction(self, user_id, run_id, interaction_id, value):
        from app.runtime.repositories import OwnedResourceNotFound
        now = _now()
        with self.database.transaction() as conn:
            row = conn.execute(select(runs).where(runs.c.id == run_id, runs.c.user_id == user_id).with_for_update()).mappings().first()
            if row is None:
                raise OwnedResourceNotFound("run not found")
            duplicate = conn.execute(select(run_interaction_responses).where(
                run_interaction_responses.c.run_id == run_id,
                run_interaction_responses.c.interaction_id == interaction_id,
            )).mappings().first()
            if duplicate:
                result = _decode_run(row)
                if duplicate["message_id"]:
                    message_row = conn.execute(select(messages).where(messages.c.id == duplicate["message_id"])).mappings().first()
                    if message_row:
                        result["accepted_message"] = _decode_message(message_row)
                return result, False
            if row["status"] != RunStatus.WAITING_USER.value:
                raise ValueError("run is not waiting for user input")
            if row["outstanding_interaction_id"] != interaction_id:
                raise ValueError("stale or mismatched interaction")
            message_id = None
            if row["conversation_id"]:
                owner = conn.execute(select(conversations.c.status).where(
                    conversations.c.id == row["conversation_id"], conversations.c.user_id == user_id,
                ).with_for_update()).scalar_one_or_none()
                if owner is None:
                    raise OwnedResourceNotFound("conversation not found")
                if owner == "archived":
                    raise ArchivedConversationError("conversation_archived")
                message_id = str(uuid.uuid4())
                sequence = int(conn.execute(select(func.coalesce(func.max(messages.c.sequence), 0) + 1).where(
                    messages.c.conversation_id == row["conversation_id"],
                )).scalar_one())
                content = value.strip() if isinstance(value, str) else _json(value)
                conn.execute(insert(messages).values(id=message_id, conversation_id=row["conversation_id"],
                    user_id=user_id, role="user", content=content, artifacts_json="[]", sequence=sequence,
                    related_run_id=run_id, related_itinerary_id=None, created_at=now))
                conn.execute(update(conversations).where(conversations.c.id == row["conversation_id"]).values(updated_at=now))
            conn.execute(insert(run_interaction_responses).values(
                run_id=run_id, interaction_id=interaction_id, message_id=message_id,
                value_json=_json(value), created_at=now,
            ))
            conn.execute(update(runs).where(runs.c.id == run_id).values(
                status=RunStatus.RUNNING.value, outstanding_interaction_id=None, updated_at=now,
            ))
            resumed = conn.execute(select(runs).where(runs.c.id == run_id)).mappings().one()
            message_row = conn.execute(select(messages).where(messages.c.id == message_id)).mappings().first() if message_id else None
            events = self._append_events(conn, run_id,
                [("custom", {"kind": "run.status", "status": RunStatus.RUNNING.value})], now)
        result = _decode_run(resumed)
        result["_durable_events"] = events
        if message_row:
            result["accepted_message"] = _decode_message(message_row)
        return result, True

    def queued(self, limit=100):
        with self.database.transaction() as conn:
            rows = conn.execute(select(runs).where(runs.c.status == "queued").order_by(runs.c.queued_at, runs.c.id).limit(limit)).mappings().all()
        return [_decode_run(row) for row in rows]

    def orphaned_active(self):
        with self.database.transaction() as conn:
            rows = conn.execute(select(runs).where(runs.c.status.in_(["running", "waiting_user"])).order_by(runs.c.updated_at, runs.c.id)).mappings().all()
        return [_decode_run(row) for row in rows]


class MySQLRunEventRepository:
    def __init__(self, database):
        self.database = database

    def append(self, run_id, kind, payload, *, durable=True):
        from app.runtime.repositories import OwnedResourceNotFound
        event_id, now = str(uuid.uuid4()), _now()
        with self.database.transaction() as conn:
            run = conn.execute(select(runs.c.id).where(runs.c.id == run_id).with_for_update()).first()
            if run is None:
                raise OwnedResourceNotFound("run not found")
            sequence = int(conn.execute(select(func.coalesce(func.max(run_events.c.sequence), 0) + 1).where(run_events.c.run_id == run_id)).scalar_one())
            conn.execute(insert(run_events).values(id=event_id, run_id=run_id, sequence=sequence,
                kind=kind, payload_json=_json(payload), durable=int(durable), created_at=now))
        return {"id": event_id, "run_id": run_id, "sequence": sequence,
                "kind": kind, "payload": payload, "durable": durable, "created_at": now}

    def after(self, run_id, after_sequence=0, limit=500):
        with self.database.transaction() as conn:
            rows = conn.execute(select(run_events).where(
                run_events.c.run_id == run_id, run_events.c.sequence > max(0, after_sequence),
            ).order_by(run_events.c.sequence).limit(max(1, min(limit, 1000)))).mappings().all()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = _loads(item.pop("payload_json"), {})
            item["durable"] = bool(item["durable"])
            return_value = item
            result.append(return_value)
        return result


class MySQLPlanningBriefRepository:
    ACTIVE = {"collecting", "ready"}
    ALLOWED = {
        "collecting": {"collecting", "ready", "discarded"},
        "ready": {"collecting", "ready", "submitted", "discarded"},
        "submitted": {"submitted"}, "discarded": {"discarded"},
    }

    def __init__(self, database):
        self.database = database

    @staticmethod
    def required_missing(data):
        from app.core.planning_brief import required_brief_fields
        return required_brief_fields(data)

    def _decode(self, row):
        return _decode_brief(row)

    def _present(self, result, conn=None):
        from app.core.mysql_travel_memory import CoreConversationMemoryRepository
        memory = CoreConversationMemoryRepository(self.database).get(
            result["user_id"], result["conversation_id"], conn,
        )
        view = build_brief_projection(result["data"], revision=memory.get("profile_revision", 0),
            frozen_facts=memory.get("profile_snapshot") or [], projection=result.get("memory_projection"),
            match_status=result.get("memory_match_status", "none"), error_code=result.get("memory_match_error_code"))
        return {**result, **view}

    def get(self, user_id, brief_id):
        from app.runtime.repositories import OwnedResourceNotFound
        with self.database.transaction() as conn:
            row = conn.execute(select(planning_briefs).where(
                planning_briefs.c.id == brief_id, planning_briefs.c.user_id == user_id,
            )).mappings().first()
        if row is None:
            raise OwnedResourceNotFound("planning brief not found")
        return self._present(self._decode(row))

    def active_for_conversation(self, user_id, conversation_id):
        with self.database.transaction() as conn:
            row = conn.execute(select(planning_briefs).where(
                planning_briefs.c.conversation_id == conversation_id,
                planning_briefs.c.user_id == user_id, planning_briefs.c.status.in_(self.ACTIVE),
            ).order_by(planning_briefs.c.updated_at.desc()).limit(1)).mappings().first()
        return self._present(self._decode(row)) if row else None

    def latest_for_conversation(self, user_id, conversation_id):
        with self.database.transaction() as conn:
            row = conn.execute(select(planning_briefs).where(
                planning_briefs.c.conversation_id == conversation_id, planning_briefs.c.user_id == user_id,
            ).order_by(planning_briefs.c.updated_at.desc(), planning_briefs.c.id.desc()).limit(1)).mappings().first()
        return self._present(self._decode(row)) if row else None

    def upsert_active(self, user_id, conversation_id, patch):
        from app.runtime.repositories import OwnedResourceNotFound
        now = _now()
        with self.database.transaction() as conn:
            owner = conn.execute(select(conversations.c.id).where(
                conversations.c.id == conversation_id, conversations.c.user_id == user_id,
                conversations.c.status == "active",
            ).with_for_update()).first()
            if not owner:
                raise OwnedResourceNotFound("conversation not found")
            from app.core.mysql_travel_memory import CoreConversationMemoryRepository
            CoreConversationMemoryRepository(self.database).ensure_snapshot(user_id, conversation_id, conn)
            row = conn.execute(select(planning_briefs).where(
                planning_briefs.c.conversation_id == conversation_id,
                planning_briefs.c.user_id == user_id, planning_briefs.c.status.in_(self.ACTIVE),
            ).with_for_update()).mappings().first()
            data = normalize_brief_data(_loads(row["data_json"], {}) if row else {})
            data.update({key: value for key, value in patch.items() if value is not None})
            data = normalize_brief_data(data)
            missing = self.required_missing(data)
            status = "collecting" if missing else "ready"
            if row:
                brief_id = row["id"]
                conn.execute(update(planning_briefs).where(planning_briefs.c.id == brief_id).values(
                    status=status, data_json=_json(data), missing_fields_json=_json(missing), updated_at=now,
                ))
            else:
                brief_id = str(uuid.uuid4())
                conn.execute(insert(planning_briefs).values(
                    id=brief_id, conversation_id=conversation_id, user_id=user_id, status=status,
                    data_json=_json(data), missing_fields_json=_json(missing),
                    memory_match_status="none", created_at=now, updated_at=now,
                ))
                conn.execute(insert(active_brief_slots).values(
                    conversation_id=conversation_id, brief_id=brief_id,
                ))
        return self.get(user_id, brief_id)

    def begin_memory_match(self, user_id, brief_id, fingerprint):
        from app.runtime.repositories import OwnedResourceNotFound
        now = _now()
        with self.database.transaction() as conn:
            result = conn.execute(update(planning_briefs).where(
                planning_briefs.c.id == brief_id, planning_briefs.c.user_id == user_id,
                planning_briefs.c.status.in_(self.ACTIVE),
            ).values(memory_match_status="pending", memory_match_error_code=None,
                memory_context_fingerprint=fingerprint, updated_at=now))
        if result.rowcount != 1:
            raise OwnedResourceNotFound("active planning brief not found")
        return self.get(user_id, brief_id)

    def complete_memory_match(self, user_id, brief_id, fingerprint, projection):
        now = _now()
        with self.database.transaction() as conn:
            result = conn.execute(update(planning_briefs).where(
                planning_briefs.c.id == brief_id, planning_briefs.c.user_id == user_id,
                planning_briefs.c.memory_context_fingerprint == fingerprint,
                planning_briefs.c.status.in_(self.ACTIVE),
            ).values(memory_projection_json=_json(projection), memory_match_status="succeeded",
                memory_match_error_code=None, memory_matched_at=now, updated_at=now))
        return result.rowcount == 1

    def fail_memory_match(self, user_id, brief_id, fingerprint, error_code):
        now = _now()
        with self.database.transaction() as conn:
            result = conn.execute(update(planning_briefs).where(
                planning_briefs.c.id == brief_id, planning_briefs.c.user_id == user_id,
                planning_briefs.c.memory_context_fingerprint == fingerprint,
                planning_briefs.c.status.in_(self.ACTIVE),
            ).values(memory_match_status="failed", memory_match_error_code=error_code, updated_at=now))
        return result.rowcount == 1

    def transition(self, user_id, brief_id, target):
        from app.runtime.repositories import OwnedResourceNotFound
        with self.database.transaction() as conn:
            row = conn.execute(select(planning_briefs).where(
                planning_briefs.c.id == brief_id, planning_briefs.c.user_id == user_id,
            ).with_for_update()).mappings().first()
            if row is None:
                raise OwnedResourceNotFound("planning brief not found")
            if target not in self.ALLOWED[row["status"]]:
                raise ValueError(f"invalid brief transition: {row['status']} -> {target}")
            if target == "submitted":
                raise ValueError("use submit() to preserve immutable snapshot")
            conn.execute(update(planning_briefs).where(planning_briefs.c.id == brief_id).values(status=target, updated_at=_now()))
            if target not in self.ACTIVE:
                conn.execute(active_brief_slots.delete().where(active_brief_slots.c.brief_id == brief_id))
        return self.get(user_id, brief_id)

    def submit(self, user_id, brief_id, create_run):
        from app.runtime.repositories import OwnedResourceNotFound
        with self.database.transaction() as conn:
            conversation_id = conn.execute(select(planning_briefs.c.conversation_id).where(
                planning_briefs.c.id == brief_id, planning_briefs.c.user_id == user_id,
            )).scalar_one_or_none()
            if conversation_id is None:
                raise OwnedResourceNotFound("planning brief not found")
            owner = conn.execute(select(conversations.c.status).where(
                conversations.c.id == conversation_id, conversations.c.user_id == user_id,
            ).with_for_update()).scalar_one_or_none()
            if owner is None:
                raise OwnedResourceNotFound("conversation not found")
            row = conn.execute(select(planning_briefs).where(
                planning_briefs.c.id == brief_id, planning_briefs.c.user_id == user_id,
            ).with_for_update()).mappings().first()
            if row is None:
                raise OwnedResourceNotFound("planning brief not found")
            if row["status"] == "submitted" and row["submitted_run_id"]:
                # The initial brief lookup established a REPEATABLE READ
                # snapshot before waiting for the conversation lock. Use a
                # current read for the Run committed by the winning submit.
                run = conn.execute(select(runs).where(
                    runs.c.id == row["submitted_run_id"], runs.c.user_id == user_id,
                    runs.c.conversation_id == conversation_id,
                ).with_for_update()).mappings().first()
                updated = dict(row)
                run = _decode_run(run)
            else:
                if row["status"] != "ready":
                    raise ValueError("planning brief is not ready")
                decoded = self._decode(row)
                memory_row = conn.execute(select(conversation_memory_states).where(
                    conversation_memory_states.c.conversation_id == conversation_id,
                    conversation_memory_states.c.user_id == user_id,
                )).mappings().first()
                memory = {"profile_revision": int(memory_row["profile_revision"]),
                          "profile_snapshot": _loads(memory_row["profile_snapshot_json"], [])} if memory_row else {
                              "profile_revision": 0, "profile_snapshot": []}
                view = build_brief_projection(decoded["data"], revision=memory["profile_revision"],
                    frozen_facts=memory["profile_snapshot"], projection=decoded.get("memory_projection"),
                    match_status=decoded.get("memory_match_status", "none"), error_code=decoded.get("memory_match_error_code"))
                snapshot = {**view["data"], "memory_context": view["memory_context"],
                    "effective_constraints": view["effective_constraints"], "constraint_coverage": view["constraint_coverage"]}
                run = create_run(conn, snapshot, conversation_id)
                now = _now()
                conn.execute(update(planning_briefs).where(planning_briefs.c.id == brief_id).values(
                    status="submitted", submission_snapshot_json=_json(snapshot), submitted_run_id=run["id"],
                    submitted_at=now, updated_at=now,
                ))
                conn.execute(active_brief_slots.delete().where(active_brief_slots.c.brief_id == brief_id))
                updated = conn.execute(select(planning_briefs).where(planning_briefs.c.id == brief_id)).mappings().one()
        return self._present(self._decode(updated)), run

    def freeze_for_run(self, user_id, brief_id, run_id):
        def existing_run(conn, _snapshot, conversation_id):
            row = conn.execute(select(runs).where(
                runs.c.id == run_id, runs.c.user_id == user_id, runs.c.conversation_id == conversation_id,
            )).mappings().first()
            return _decode_run(row)
        return self.submit(user_id, brief_id, existing_run)

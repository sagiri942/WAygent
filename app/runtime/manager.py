"""Run lifecycle service and durable-first event publication."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from app.core.database import get_conn
from app.runtime.models import (
    DisconnectPolicy,
    PublicError,
    RunKind,
    RunStatus,
    TERMINAL_STATUSES,
    concurrency_key,
)
from app.runtime.repositories import ConversationRunActive, RunEventRepository, RunRepository
from app.runtime.stream import StreamBridge, StreamItem


class RunManager:
    def __init__(
        self,
        db_path: str | Path | None = None,
        bridge: StreamBridge | None = None,
        trace=None,
    ):
        self.db_path = db_path
        self.database = db_path if getattr(getattr(db_path, "engine", None), "dialect", None) else None
        self.runs = RunRepository(db_path)
        self.events = RunEventRepository(db_path)
        self.bridge = bridge or StreamBridge()
        self._trace = trace
        self._owns_trace = trace is None
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._revision_commit_locks: dict[str, asyncio.Lock] = {}
        self._revision_commit_results: dict[str, str] = {}
        self._revision_runs: set[str] = set()
        from app.runtime.resources import PlanningResources
        self.planning_resources = PlanningResources()

    @property
    def trace(self):
        # Imported bootstrap managers do not create unused exporter threads.
        if self._trace is None:
            from app.runtime.tracing import configured_run_trace
            self._trace = configured_run_trace()
        return self._trace

    def prepare_trace(self):
        if self._owns_trace and getattr(self._trace, "closed", False):
            self._trace = None

    async def close_trace(self, timeout_ms=2000):
        if self._trace is not None:
            return await self._trace.close(timeout_ms=timeout_ms)
        return True

    def create(
        self,
        *,
        user_id: str,
        kind: RunKind | str,
        request_snapshot: dict[str, Any],
        conversation_id: str | None = None,
        itinerary_id: str | None = None,
        retry_of_run_id: str | None = None,
        disconnect_policy: DisconnectPolicy | str = DisconnectPolicy.CONTINUE,
        enforce_conversation_idle: bool = False,
    ) -> dict[str, Any]:
        run_id = str(uuid.uuid4())
        key = concurrency_key(
            kind,
            conversation_id=conversation_id,
            run_id=run_id,
            itinerary_id=itinerary_id,
        )
        if self.database is not None:
            from sqlalchemy import select
            from app.core.identity_schema import conversations as conversation_table, runs as run_table
            with self.database.transaction() as conn:
                if enforce_conversation_idle and conversation_id and RunKind(kind) is not RunKind.SPOT_TIPS:
                    owner = conn.execute(select(conversation_table.c.id).where(
                        conversation_table.c.id == conversation_id,
                        conversation_table.c.user_id == user_id,
                    ).with_for_update()).first()
                    if owner is None:
                        from app.runtime.repositories import OwnedResourceNotFound
                        raise OwnedResourceNotFound("conversation not found")
                    active = conn.execute(select(run_table).where(
                        run_table.c.conversation_id == conversation_id,
                        run_table.c.user_id == user_id,
                        run_table.c.kind.in_(["chat", "travel_plan", "revision"]),
                        run_table.c.status.in_(["queued", "running", "waiting_user"]),
                    ).order_by(run_table.c.created_at.desc(), run_table.c.id.desc()).limit(1)).mappings().first()
                    if active:
                        from app.runtime.repositories import _decode_run
                        raise ConversationRunActive(_decode_run(active))
                run = self.runs.insert(
                    conn, run_id=run_id, user_id=user_id, kind=kind,
                    concurrency_key=key, request_snapshot=request_snapshot,
                    conversation_id=conversation_id, retry_of_run_id=retry_of_run_id,
                    disconnect_policy=disconnect_policy,
                )
            self.trace.queued(run)
            return run
        with get_conn(self.db_path) as conn:
            if enforce_conversation_idle and conversation_id and RunKind(kind) is not RunKind.SPOT_TIPS:
                conn.execute("BEGIN IMMEDIATE")
                active = conn.execute(
                    "SELECT * FROM runs WHERE conversation_id=? AND user_id=? "
                    "AND kind IN ('chat','travel_plan','revision') "
                    "AND status IN ('queued','running','waiting_user') "
                    "ORDER BY created_at DESC,id DESC LIMIT 1",
                    (conversation_id, user_id),
                ).fetchone()
                if active:
                    raise ConversationRunActive(dict(active))
            run = self.runs.insert(
                conn,
                run_id=run_id,
                user_id=user_id,
                kind=kind,
                concurrency_key=key,
                request_snapshot=request_snapshot,
                conversation_id=conversation_id,
                retry_of_run_id=retry_of_run_id,
                disconnect_policy=disconnect_policy,
            )
        self.trace.queued(run)
        return run

    def retry(self, user_id: str, run_id: str) -> dict[str, Any]:
        original = self.runs.get(user_id, run_id)
        if RunStatus(original["status"]) not in {
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }:
            raise ValueError("only failed or cancelled runs can be retried")
        itinerary_id = None
        if original["kind"] == RunKind.REVISION.value:
            raise ValueError("legacy revision Runs cannot be retried; submit a newly confirmed revision in chat")
        if original["kind"] == RunKind.SPOT_TIPS.value:
            itinerary_id = original["request_snapshot"].get("itinerary_id")
        return self.create(
            user_id=user_id,
            kind=original["kind"],
            request_snapshot=original["request_snapshot"],
            conversation_id=original["conversation_id"],
            itinerary_id=itinerary_id,
            retry_of_run_id=run_id,
            disconnect_policy=original["disconnect_policy"],
            enforce_conversation_idle=True,
        )

    async def publish(
        self,
        run_id: str,
        kind: str,
        payload: dict[str, Any],
        *,
        durable: bool = True,
        validate_custom: bool = True,
    ) -> StreamItem:
        sequence = None
        if durable:
            event = await asyncio.to_thread(
                self.events.append, run_id, kind, payload, durable=True
            )
            sequence = event["sequence"]
        item = StreamItem(run_id, kind, payload, sequence, durable)
        await self.bridge.publish(item)
        if kind == "messages" or (kind == "custom" and payload.get("kind") not in {"run.status", None}):
            self.trace.progress(run_id)
        return item

    async def publish_itinerary(self, itinerary_id: str, payload: dict[str, Any]) -> None:
        """Live-only detail-page notification; durable state remains in SQLite."""
        await self.bridge.publish(StreamItem(
            f"itinerary:{itinerary_id}", "custom", payload, durable=False
        ))

    async def transition(
        self,
        run_id: str,
        target: RunStatus | str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        run = await asyncio.to_thread(self.runs.transition, run_id, target, **kwargs)
        self.trace.status(run)
        if RunStatus(run["status"]) in TERMINAL_STATUSES:
            self._revision_commit_results.pop(run_id, None)
            self._revision_runs.discard(run_id)
        if self.database is not None:
            persisted = run.pop("_durable_events", [])
            for event in persisted:
                await self.bridge.publish(StreamItem(
                    run_id, event["kind"], event["payload"], event["sequence"], True,
                ))
            return run
        await self.publish(
            run_id,
            "custom",
            {"kind": "run.status", "status": run["status"]},
            durable=True,
        )
        if RunStatus(run["status"]) is RunStatus.FAILED and run["error_public"]:
            await self.publish(
                run_id,
                "error",
                run["error_public"],
                durable=True,
            )
        if RunStatus(run["status"]) in TERMINAL_STATUSES:
            await self.publish(
                run_id,
                "end",
                {"status": run["status"]},
                durable=True,
            )
        return run

    async def cancel(self, user_id: str, run_id: str) -> dict[str, Any]:
        run = await asyncio.to_thread(self.runs.get, user_id, run_id)
        current = RunStatus(run["status"])
        if current is RunStatus.CANCELLED:
            return run
        if current in TERMINAL_STATUSES:
            raise ValueError("completed run cannot be cancelled")
        from app.planning.revision_timeout import is_revision_request
        is_revision = run_id in self._revision_runs or is_revision_request(run.get("request_snapshot") or {})
        # Every cancellation fences persistence, including normalized multi-turn tools.
        lock = self._revision_commit_locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            latest = await asyncio.to_thread(self.runs.get_internal, run_id)
            if RunStatus(latest["status"]) in TERMINAL_STATUSES:
                result = latest
            else:
                committed = self._revision_commit_results.get(run_id)
                if committed:
                    result = await self.transition(
                        run_id, RunStatus.SUCCEEDED, result_itinerary_id=committed,
                    )
                else:
                    result = await self._cancel_run(run_id, revision=is_revision)
        self._revision_commit_locks.pop(run_id, None)
        return result

    async def _cancel_run(self, run_id: str, *, revision: bool) -> dict[str, Any]:
        cancel_event = self._cancel_events.get(run_id)
        if cancel_event:
            cancel_event.set()
        task = self._tasks.get(run_id)
        if task:
            task.cancel()
        error = (
            PublicError(
                code="revision_cancelled",
                message="行程修改已取消，未提交新版本；原行程保持不变。",
                retryable=True,
            ).model_dump()
            if revision else None
        )
        result = await self.transition(run_id, RunStatus.CANCELLED, error_public=error)
        return result

    @asynccontextmanager
    async def revision_execution(self, run: dict[str, Any], itinerary_id: str):
        from app.runtime.resources import suspend_chat_capacity
        from app.planning.revision_timeout import (
            RevisionTimeoutError, activate_runtime_deadline, remaining_deadline_seconds,
            remaining_revision_seconds, revision_deadline_scope,
        )
        self._revision_runs.add(run["id"])
        remaining = remaining_revision_seconds(run)
        inherited = remaining_deadline_seconds()
        if inherited is not None:
            remaining = min(remaining, inherited)
        if remaining <= 0:
            raise RevisionTimeoutError()
        deadline = asyncio.get_running_loop().time() + remaining
        activate_runtime_deadline(deadline)
        scope = asyncio.timeout_at(deadline)
        try:
            with revision_deadline_scope(deadline):
                async with scope:
                    async with suspend_chat_capacity(deadline), self.planning_resources.revision(run["user_id"], itinerary_id):
                        current = await asyncio.to_thread(self.runs.get_internal, run["id"])
                        if RunStatus(current["status"]) in TERMINAL_STATUSES:
                            raise asyncio.CancelledError
                        yield
        except TimeoutError as exc:
            if scope.expired():
                raise RevisionTimeoutError() from exc
            raise

    @asynccontextmanager
    async def revision_commit(self, run_id: str):
        """Serialize cancellation against the irreversible itinerary commit."""
        self._revision_runs.add(run_id)
        lock = self._revision_commit_locks.setdefault(run_id, asyncio.Lock())
        await lock.acquire()
        terminal = False
        try:
            if run_id in self._revision_commit_results:
                raise RuntimeError("revision has already committed")
            current = await asyncio.to_thread(self.runs.get_internal, run_id)
            if RunStatus(current["status"]) in TERMINAL_STATUSES:
                terminal = True
                raise asyncio.CancelledError
            cancel_event = self._cancel_events.get(run_id)
            if cancel_event is not None and cancel_event.is_set():
                raise asyncio.CancelledError
            yield
        finally:
            # Remove a terminal Run's fence while still holding it. A later
            # rejected attempt may create a fresh lock, but its terminal-state
            # check will stop it before persistence can begin.
            if terminal and self._revision_commit_locks.get(run_id) is lock:
                self._revision_commit_locks.pop(run_id, None)
            lock.release()

    def mark_revision_committed(self, run_id: str, itinerary_id: str) -> None:
        """Record the commit while its revision_commit lock is still held."""
        self._revision_commit_results[run_id] = itinerary_id

    async def persist_revision(self, run_id: str, persist) -> str:
        """Commit a revision without letting task cancellation orphan its DB thread."""
        from app.planning.revision_timeout import remaining_deadline_seconds

        remaining = remaining_deadline_seconds()
        if remaining is not None and remaining <= 0:
            raise TimeoutError("revision deadline expired before itinerary persistence")
        async with self.revision_commit(run_id):
            remaining = remaining_deadline_seconds()
            if remaining is not None and remaining <= 0:
                raise TimeoutError("revision deadline expired at the commit fence")
            task = asyncio.create_task(asyncio.to_thread(persist))
            try:
                itinerary_id = await asyncio.shield(task)
            except asyncio.CancelledError:
                # A driver call in a worker thread cannot be safely interrupted.
                # Keep the cancellation fence until its transactional outcome is known.
                try:
                    itinerary_id = await task
                except BaseException as exc:
                    remaining = remaining_deadline_seconds()
                    if remaining is not None and remaining <= 0:
                        raise asyncio.CancelledError from exc
                    raise
                self.mark_revision_committed(run_id, str(itinerary_id))
                # The durable commit won the race. Continue the success path so
                # its itinerary event and final Run result agree with storage.
                return str(itinerary_id)
            self.mark_revision_committed(run_id, str(itinerary_id))
            return str(itinerary_id)

    def register_task(
        self, run_id: str, task: asyncio.Task[Any], cancel_event: asyncio.Event
    ) -> None:
        self._tasks[run_id] = task
        self._cancel_events[run_id] = cancel_event

        def cleanup(_task: asyncio.Task[Any]) -> None:
            self._tasks.pop(run_id, None)
            self._cancel_events.pop(run_id, None)
            self._revision_commit_locks.pop(run_id, None)

        task.add_done_callback(cleanup)

    async def reconcile_startup(self) -> list[str]:
        reconciled = []
        for run in await asyncio.to_thread(self.runs.orphaned_active):
            await self.transition(
                run["id"],
                RunStatus.FAILED,
                error_public=PublicError(
                    code="server_restarted",
                    message="服务重启导致任务中断，请重试",
                    retryable=True,
                ).model_dump(),
                error_internal="orphaned run found during single-node startup reconciliation",
            )
            reconciled.append(run["id"])
        return reconciled

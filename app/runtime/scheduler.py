"""Deterministic bounded scheduler for independently executing runs."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from app.runtime.manager import RunManager
from app.runtime.resources import ChatCapacityLease, PlanningResources, chat_capacity_scope
from app.runtime.models import PublicError, RunKind, RunStatus
from app.runtime.observability import metrics

RunHandler = Callable[[dict[str, Any], asyncio.Event], Awaitable[dict[str, Any] | None]]


def public_error_for_exception(exc: Exception) -> PublicError:
    """Translate provider failures without exposing credentials or raw payloads."""
    explicit_code = getattr(exc, "public_code", None)
    explicit_message = getattr(exc, "public_message", None)
    if explicit_code or explicit_message:
        return PublicError(
            code=str(explicit_code or "run_failed"),
            message=str(explicit_message or "任务执行失败，请稍后重试"),
            retryable=bool(getattr(exc, "public_retryable", True)),
        )

    status_code = getattr(exc, "status_code", None)
    normalized = str(exc).casefold()
    if status_code == 402 or "insufficient balance" in normalized:
        return PublicError(
            code="llm_balance_exhausted",
            message="模型服务余额不足，请充值或切换可用模型后再试。",
            retryable=False,
        )
    if status_code in {401, 403}:
        return PublicError(
            code="llm_authentication_failed",
            message="模型服务认证失败，请检查 API Key 和模型权限。",
            retryable=False,
        )
    if status_code == 429:
        exhausted = "insufficient_quota" in normalized or "quota" in normalized
        return PublicError(
            code="llm_quota_exhausted" if exhausted else "llm_rate_limited",
            message=(
                "模型服务额度已用尽，请补充额度或切换可用模型后再试。"
                if exhausted
                else "模型服务当前请求过多，请稍后重试。"
            ),
            retryable=not exhausted,
        )
    return PublicError()


class RuntimeScheduler:
    def __init__(
        self,
        manager: RunManager,
        *,
        chat_limit: int = 8,
        planning_limit: int = 2,
        planning_per_user: int = 2,
        llm_limit: int = 8,
        amap_limit: int = 8,
    ):
        self.manager = manager
        self.chat_capacity = asyncio.Semaphore(max(1, chat_limit))
        manager.planning_resources = PlanningResources(planning_limit, planning_per_user)
        self.planning_capacity = manager.planning_resources.capacity
        self.planning_per_user = max(1, planning_per_user)
        self.llm_capacity = asyncio.Semaphore(max(1, llm_limit))
        self.amap_capacity = asyncio.Semaphore(max(1, amap_limit))
        self._user_planning = manager.planning_resources.users
        self._key_locks = manager.planning_resources.keys
        self._handlers: dict[RunKind, RunHandler] = {}
        self._wake = asyncio.Event()
        self._loop_task: asyncio.Task[Any] | None = None
        self._stopping = False

    def register(self, kind: RunKind | str, handler: RunHandler) -> None:
        self._handlers[RunKind(kind)] = handler

    def notify(self) -> None:
        self._wake.set()

    async def resume(
        self,
        run: dict[str, Any],
        interaction_id: str,
        value: Any,
    ) -> dict[str, Any]:
        handler = self._handlers.get(RunKind(run["kind"]))
        if not handler or not hasattr(handler, "resume"):
            raise ValueError("run handler does not support resume")
        resumed, accepted = await asyncio.to_thread(
            self.manager.runs.accept_interaction,
            run["user_id"], run["id"], interaction_id, value,
        )
        if not accepted:
            return resumed
        if getattr(self.manager, "database", None) is not None:
            for event in resumed.pop("_durable_events", []):
                from app.runtime.stream import StreamItem
                await self.manager.bridge.publish(StreamItem(
                    run["id"], event["kind"], event["payload"], event["sequence"], True,
                ))
        else:
            await self.manager.publish(
                run["id"], "custom", {"kind": "run.status", "status": "running"},
                durable=True,
            )
        cancel_event = asyncio.Event()
        self.manager.trace.resume_queued(resumed)
        task = asyncio.create_task(
            self._resume_execute(resumed, cancel_event, handler, value),
            name=f"resume:{run['id']}",
        )
        self.manager.register_task(run["id"], task, cancel_event)
        return resumed

    async def start(self) -> None:
        if self._loop_task and not self._loop_task.done():
            return
        self._stopping = False
        self.manager.prepare_trace()
        await self.manager.reconcile_startup()
        self._loop_task = asyncio.create_task(self._loop(), name="agent-runtime-scheduler")
        self.notify()

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        if self._loop_task:
            await self._loop_task
        tasks = list(self.manager._tasks.values())
        if tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True), timeout=2
                )
            except TimeoutError:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def _loop(self) -> None:
        while not self._stopping:
            await self._wake.wait()
            self._wake.clear()
            for run in await asyncio.to_thread(self.manager.runs.queued):
                if run["id"] in self.manager._tasks:
                    continue
                cancel_event = asyncio.Event()
                task = asyncio.create_task(
                    self._execute(run, cancel_event), name=f"run:{run['id']}"
                )
                self.manager.register_task(run["id"], task, cancel_event)
            await asyncio.sleep(0)

    async def _execute(
        self, run: dict[str, Any], cancel_event: asyncio.Event,
        *, resumed: bool = False, handler=None, value=None,
    ) -> None:
        self.manager.trace.queued(run)
        kind = RunKind(run["kind"])
        handler = handler or self._handlers.get(kind)
        if not handler:
            is_retired_revision = kind is RunKind.REVISION
            await self.manager.transition(
                run["id"],
                RunStatus.FAILED,
                error_public=PublicError(
                    code="legacy_revision_run_retired" if is_retired_revision else "handler_missing",
                    message=(
                        "旧版修订任务已停止执行。请在对话中重新确认并提交修改要求。"
                        if is_retired_revision else "该任务类型暂不可用"
                    ),
                    retryable=False,
                ).model_dump(),
                error_internal=(
                    "legacy revision run has no worker after Ticket21 migration"
                    if is_retired_revision else f"no handler registered for {kind.value}"
                ),
            )
            return
        capacity = (
            ChatCapacityLease(self.chat_capacity)
            if kind is RunKind.CHAT
            else self.planning_capacity
        )
        user_capacity = (
            self._user_planning[run["user_id"]]
            if kind in {RunKind.TRAVEL_PLAN, RunKind.REVISION, RunKind.SPOT_TIPS}
            else _NullAsyncContext()
        )
        key_lock = self._key_locks[run["concurrency_key"]]
        revision_timeout_scope = None
        try:
            from app.planning.revision_timeout import (
                is_revision_request, remaining_revision_seconds,
                revision_deadline_scope, runtime_timeout_scope,
            )

            async def execute_with_capacity() -> None:
                # A queued same-conversation turn must not hold a Chat slot while
                # the active turn lends its slot to an inline planning tool.
                conversation_lock = key_lock if kind is RunKind.CHAT else _NullAsyncContext()
                planning_lock = _NullAsyncContext() if kind is RunKind.CHAT else key_lock
                async with conversation_lock, capacity, user_capacity, planning_lock:
                    fresh = await asyncio.to_thread(
                        self.manager.runs.get_internal, run["id"]
                    )
                    expected = RunStatus.RUNNING.value if resumed else RunStatus.QUEUED.value
                    if fresh["status"] != expected:
                        return
                    if not resumed:
                        await self.manager.transition(run["id"], RunStatus.RUNNING)
                    metrics.run_started(fresh)
                    with self.manager.trace.execution(fresh, resumed=resumed), chat_capacity_scope(
                        capacity if kind is RunKind.CHAT else None,
                    ):
                        result = (await handler.resume(fresh, cancel_event, value) if resumed
                                  else await handler(fresh, cancel_event))
                    latest = await asyncio.to_thread(
                        self.manager.runs.get_internal, run["id"]
                    )
                    if latest["status"] == RunStatus.RUNNING.value:
                        await self.manager.transition(
                            run["id"],
                            RunStatus.SUCCEEDED,
                            result_itinerary_id=(result or {}).get("result_itinerary_id"),
                        )
                        metrics.run_finished(fresh, RunStatus.SUCCEEDED.value)
                    elif latest["status"] == RunStatus.WAITING_USER.value:
                        metrics.run_finished(fresh, RunStatus.WAITING_USER.value)

            deadline = None
            if run["id"] in self.manager._revision_runs or is_revision_request(run.get("request_snapshot") or {}):
                deadline = asyncio.get_running_loop().time() + max(0.0, remaining_revision_seconds(run))
            revision_timeout_scope = asyncio.timeout_at(deadline)
            with revision_deadline_scope(deadline), runtime_timeout_scope(revision_timeout_scope):
                async with revision_timeout_scope:
                    await execute_with_capacity()
        except asyncio.CancelledError:
            latest = await asyncio.to_thread(self.manager.runs.get_internal, run["id"])
            committed = self.manager._revision_commit_results.get(run["id"])
            if committed and latest["status"] not in {
                RunStatus.SUCCEEDED.value,
                RunStatus.FAILED.value,
            }:
                await self.manager.transition(
                    run["id"], RunStatus.SUCCEEDED, result_itinerary_id=committed,
                )
                metrics.run_finished(run, RunStatus.SUCCEEDED.value)
            elif latest["status"] not in {
                RunStatus.CANCELLED.value,
                RunStatus.SUCCEEDED.value,
                RunStatus.FAILED.value,
            }:
                await self.manager.transition(run["id"], RunStatus.CANCELLED)
                metrics.run_finished(run, RunStatus.CANCELLED.value)
        except Exception as exc:  # noqa: BLE001
            latest = await asyncio.to_thread(self.manager.runs.get_internal, run["id"])
            committed = self.manager._revision_commit_results.get(run["id"])
            if committed and latest["status"] not in {
                RunStatus.SUCCEEDED.value,
                RunStatus.FAILED.value,
            }:
                await self.manager.transition(
                    run["id"], RunStatus.SUCCEEDED, result_itinerary_id=committed,
                )
                metrics.run_finished(run, RunStatus.SUCCEEDED.value)
                return
            if latest["status"] not in {
                RunStatus.CANCELLED.value,
                RunStatus.SUCCEEDED.value,
                RunStatus.FAILED.value,
            }:
                timeout_expired = bool(
                    isinstance(exc, TimeoutError)
                    and revision_timeout_scope is not None
                    and revision_timeout_scope.expired()
                )
                public_error = (
                    PublicError(
                        code="revision_timeout",
                        message="行程修改超过总时限，未提交新版本；原行程保持不变，请稍后重试。",
                        retryable=True,
                    )
                    if timeout_expired else public_error_for_exception(exc)
                )
                await self.manager.transition(
                    run["id"],
                    RunStatus.FAILED,
                    error_public=public_error.model_dump(),
                    error_internal=repr(exc),
                )
                metrics.run_finished(
                    run, RunStatus.FAILED.value,
                    failure_reason="RevisionTimeout" if timeout_expired else type(exc).__name__,
                )
        finally:
            self.notify()

    async def _resume_execute(
        self,
        run: dict[str, Any],
        cancel_event: asyncio.Event,
        handler: Any,
        value: Any,
    ) -> None:
        await self._execute(run, cancel_event, resumed=True, handler=handler, value=value)


class _NullAsyncContext:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

"""Total deadline policy for chat messages that revise a saved itinerary."""

from __future__ import annotations

import os
import asyncio
import time
from functools import wraps
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

DEFAULT_REVISION_TIMEOUT_SECONDS = 300.0
DEFAULT_PLANNING_TASK_TIMEOUT_SECONDS = 180.0
REVISION_TIMEOUT_ENV = "RUNTIME_REVISION_TIMEOUT_SECONDS"
_deadline_monotonic: ContextVar[float | None] = ContextVar("revision_deadline_monotonic", default=None)
_runtime_timeout: ContextVar[asyncio.Timeout | None] = ContextVar("revision_runtime_timeout", default=None)


class RevisionTimeoutError(TimeoutError):
    public_code = "revision_timeout"
    public_message = "行程修改超过总时限，未提交新版本；原行程保持不变，请稍后重试。"
    public_retryable = True


class PlanningExecutionTimeout(TimeoutError):
    """The bounded generation/repair task expired before persistence."""

    public_code = "planning_timeout"
    public_message = "规划与交通冲突修复超过3分钟，本次未保存行程；请稍后重试或调整安排。"
    public_retryable = True


async def run_with_planning_budget(operation, seconds: float = DEFAULT_PLANNING_TASK_TIMEOUT_SECONDS):
    """Bound graph execution and repairs, ending before any atomic save begins."""
    scope = asyncio.timeout(seconds)
    try:
        async with scope:
            return await operation
    except TimeoutError as exc:
        if scope.expired():
            raise PlanningExecutionTimeout() from exc
        raise


@contextmanager
def runtime_timeout_scope(scope: asyncio.Timeout):
    token = _runtime_timeout.set(scope)
    try:
        yield
    finally:
        _runtime_timeout.reset(token)


def activate_runtime_deadline(deadline: float) -> None:
    scope = _runtime_timeout.get()
    if scope is not None and not scope.expired():
        current = scope.when()
        scope.reschedule(deadline if current is None else min(current, deadline))


def revision_execution(method):
    """Apply policy at the actual business execution seam, after tool normalization."""
    @wraps(method)
    async def execute(service, run, itinerary_id, *args, **kwargs):
        async with service.manager.revision_execution(run, itinerary_id):
            return await method(service, run, itinerary_id, *args, **kwargs)
    return execute


def revision_timeout_seconds() -> float:
    """Return the configured total timeout, with a safe bounded default."""
    try:
        value = float(os.getenv(REVISION_TIMEOUT_ENV, str(DEFAULT_REVISION_TIMEOUT_SECONDS)))
    except (TypeError, ValueError):
        value = DEFAULT_REVISION_TIMEOUT_SECONDS
    if value <= 0:
        return DEFAULT_REVISION_TIMEOUT_SECONDS
    return min(value, 86_400.0)


def is_revision_request(snapshot: dict[str, Any]) -> bool:
    """Match the same supported local/full revision routes used by the chat tool."""
    text = str(snapshot.get("text") or "")
    from app.planning.full_revision import is_full_revision_request
    from app.planning.local_add_remove import is_add_remove_request, is_replace_request
    from app.planning.local_reorder import is_explicit_order_request

    return any((
        is_full_revision_request(text),
        is_replace_request(text),
        is_add_remove_request(text),
        is_explicit_order_request(text),
    ))


def remaining_revision_seconds(run: dict[str, Any], *, now: float | None = None) -> float:
    """Compute remaining budget from durable queue creation time, including queue wait."""
    created_at = str(run.get("created_at") or run.get("queued_at") or "")
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        started = created.timestamp()
    except (TypeError, ValueError, OverflowError):
        started = now if now is not None else time.time()
    current = time.time() if now is None else now
    return revision_timeout_seconds() - max(0.0, current - started)


@contextmanager
def revision_deadline_scope(deadline: float | None):
    token = _deadline_monotonic.set(deadline)
    try:
        yield
    finally:
        _deadline_monotonic.reset(token)


def remaining_deadline_seconds() -> float | None:
    deadline = _deadline_monotonic.get()
    return None if deadline is None else deadline - time.monotonic()

"""Application-scoped runtime wiring."""

from __future__ import annotations

import os
from collections.abc import Callable
from functools import partial
from app.chat.memory_service import MemoryExtractionWorker
from app.chat.service import ChatService
from app.core.http import close_async_http_client
from app.planning.graph import build_graph
from app.planning.runtime_worker import (
    PlanningFinalizer,
    SpotTipsWorker,
    planning_run_to_state,
)
from app.runtime.manager import RunManager
from app.runtime.models import RunKind
from app.runtime.scheduler import RuntimeScheduler
from app.runtime.worker import GraphRuntimeWorker

manager = RunManager()
chat_service = ChatService(manager)
scheduler = RuntimeScheduler(
    manager,
    chat_limit=int(os.getenv("RUNTIME_CHAT_CONCURRENCY", "8")),
    planning_limit=int(os.getenv("RUNTIME_PLANNING_CONCURRENCY", "2")),
    planning_per_user=int(os.getenv("RUNTIME_PLANNING_PER_USER", "2")),
    llm_limit=int(os.getenv("RUNTIME_LLM_CONCURRENCY", "8")),
    amap_limit=int(os.getenv("RUNTIME_AMAP_CONCURRENCY", "8")),
)

checkpointer = None
chat_worker: GraphRuntimeWorker | None = None
planning_worker: GraphRuntimeWorker | None = None
spot_tips_worker: SpotTipsWorker | None = None
memory_worker = MemoryExtractionWorker(manager.db_path)
_checkpoint_context = None
_checkpoint_context_factory = None


def configure_runtime_storage(
    database,
    checkpoint_context_factory: Callable[[], object],
    *,
    manager_backend=None,
    scheduler_backend=None,
    chat_service_backend=None,
) -> None:
    """Bind the application Runtime to explicit production storage."""
    global manager, scheduler, chat_service, memory_worker, _checkpoint_context_factory
    if database.engine.dialect.name != "mysql":
        raise RuntimeError("Production Runtime requires MySQL business storage")
    if manager_backend is None or scheduler_backend is None or chat_service_backend is None:
        from app.api.runtime_routes import configure_runtime_backend
        manager_backend, scheduler_backend, chat_service_backend = configure_runtime_backend(database)
    manager = manager_backend
    scheduler = scheduler_backend
    chat_service = chat_service_backend
    memory_worker = MemoryExtractionWorker(database)
    _checkpoint_context_factory = checkpoint_context_factory


async def start_runtime(*, checkpoint_context_factory: Callable[[], object] | None = None) -> None:
    global checkpointer, chat_worker, planning_worker, spot_tips_worker, _checkpoint_context
    if _checkpoint_context is not None:
        raise RuntimeError("runtime is already started")
    factory = checkpoint_context_factory or _checkpoint_context_factory
    if factory is None:
        raise RuntimeError("Runtime checkpoint saver is not configured; SQLite fallback is disabled")
    context = factory()
    _checkpoint_context = context
    scheduler_started = False
    memory_started = False
    try:
        checkpointer = await context.__aenter__()
        await checkpointer.setup()
        from app.chat.react_graph import build_main_agent_graph
        from app.chat.tool_service import MainAgentToolService
        from app.chat.tools import build_main_agent_tools

        tools = build_main_agent_tools(MainAgentToolService(chat_service))
        chat_worker = GraphRuntimeWorker(
            manager,
            build_main_agent_graph(tools, checkpointer=checkpointer),
            chat_service.react_chat_input,
            stream_messages=True,
            stream_tools=True,
            visible_nodes={"model"},
            context_builder=chat_service.main_agent_context,
            finalizer=chat_service.finalize_react_chat,
        )
        planning_worker = GraphRuntimeWorker(
            manager,
            build_graph(
                memory_writer=None,
                checkpointer=checkpointer,
                interrupt_on_missing=False,
            ),
            partial(
                planning_run_to_state,
                memory_repository=memory_worker.facts,
            ),
            stream_messages=False,
            finalizer=PlanningFinalizer(manager, scheduler.notify),
        )
        spot_tips_worker = SpotTipsWorker(manager)
        scheduler.register(RunKind.CHAT, chat_worker)
        scheduler.register(RunKind.TRAVEL_PLAN, planning_worker)
        scheduler.register(RunKind.SPOT_TIPS, spot_tips_worker)
        scheduler_started = True
        await scheduler.start()
        memory_started = True
        await memory_worker.start()
    except BaseException as error:
        cleanup_errors: list[BaseException] = []
        for started, shutdown in ((memory_started, memory_worker.stop), (scheduler_started, scheduler.stop)):
            if started:
                try:
                    await shutdown()
                except BaseException as cleanup_error:
                    cleanup_errors.append(cleanup_error)
        try:
            await manager.close_trace(timeout_ms=int(os.getenv("RUNTIME_TRACE_FLUSH_TIMEOUT_MS", "2000")))
        except BaseException as cleanup_error:
            cleanup_errors.append(cleanup_error)
        try:
            if checkpointer is not None:
                await context.__aexit__(type(error), error, error.__traceback__)
        except BaseException as cleanup_error:
            cleanup_errors.append(cleanup_error)
        finally:
            _checkpoint_context = None
            checkpointer = None
            chat_worker = None
            planning_worker = None
            spot_tips_worker = None
        if cleanup_errors:
            error.add_note("Runtime cleanup also failed: " + ", ".join(type(item).__name__ for item in cleanup_errors))
        raise


async def stop_runtime() -> None:
    global checkpointer, chat_worker, planning_worker, spot_tips_worker, _checkpoint_context
    first_error: BaseException | None = None
    for shutdown in (memory_worker.stop, scheduler.stop, close_async_http_client):
        try:
            await shutdown()
        except BaseException as error:
            if first_error is None:
                first_error = error
    try:
        await manager.close_trace(timeout_ms=int(os.getenv("RUNTIME_TRACE_FLUSH_TIMEOUT_MS", "2000")))
    except Exception:
        from app.runtime.tracing import logger
        logger.warning("run_trace_shutdown_failed")
    context = _checkpoint_context
    try:
        if context is not None:
            try:
                await context.__aexit__(None, None, None)
            except BaseException as error:
                if first_error is None:
                    first_error = error
    finally:
        _checkpoint_context = None
        checkpointer = None
        chat_worker = None
        planning_worker = None
        spot_tips_worker = None
    if first_error is not None:
        raise first_error

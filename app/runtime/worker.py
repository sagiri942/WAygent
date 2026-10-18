"""LangGraph v2 stream consumer that exposes only the public protocol."""

from __future__ import annotations

import asyncio
import inspect
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import TypeAdapter, ValidationError

from app.runtime.manager import RunManager
from app.runtime.models import CUSTOM_EVENT_TYPES, RunStatus
from app.runtime.observability import metrics
from langgraph.types import Command

InputBuilder = Callable[[dict[str, Any]], dict[str, Any] | Any]
ContextBuilder = Callable[[dict[str, Any]], dict[str, Any] | Any]
Finalizer = Callable[
    [dict[str, Any], dict[str, Any], str],
    Awaitable[dict[str, Any] | None],
]

_custom_adapter = TypeAdapter(CUSTOM_EVENT_TYPES)


class GraphRuntimeWorker:
    """Execute a compiled graph with `astream(..., version="v2")`.

    Raw updates are consumed only for final state and interrupt detection. They are
    never published to clients.
    """

    def __init__(
        self,
        manager: RunManager,
        graph: Any,
        input_builder: InputBuilder,
        *,
        stream_messages: bool,
        visible_nodes: set[str] | None = None,
        visible_tags: set[str] | None = None,
        finalizer: Finalizer | None = None,
        context_builder: ContextBuilder | None = None,
        stream_tools: bool = False,
    ):
        self.manager = manager
        self.graph = graph
        self.input_builder = input_builder
        self.stream_messages = stream_messages
        self.visible_nodes = visible_nodes or {"respond"}
        self.visible_tags = visible_tags or {"user-visible"}
        self.finalizer = finalizer
        self.context_builder = context_builder
        self.stream_tools = stream_tools

    async def __call__(
        self, run: dict[str, Any], cancel_event: asyncio.Event
    ) -> dict[str, Any] | None:
        graph_input = self.input_builder(run)
        if inspect.isawaitable(graph_input):
            graph_input = await graph_input
        context = self.context_builder(run) if self.context_builder else None
        if inspect.isawaitable(context):
            context = await context
        return await self._execute(run, cancel_event, graph_input, context=context)

    async def resume(
        self,
        run: dict[str, Any],
        cancel_event: asyncio.Event,
        value: Any,
    ) -> dict[str, Any] | None:
        context = self.context_builder(run) if self.context_builder else None
        if inspect.isawaitable(context):
            context = await context
        return await self._execute(
            run, cancel_event, Command(resume=value), context=context
        )

    async def _execute(
        self,
        run: dict[str, Any],
        cancel_event: asyncio.Event,
        graph_input: Any,
        *,
        context: Any = None,
    ) -> dict[str, Any] | None:
        started_at = time.monotonic()
        stream_modes = ["custom", "updates", "values"]
        if self.stream_messages:
            stream_modes.insert(0, "messages")
        if self.stream_tools:
            stream_modes.insert(0, "tools")
        # Top-level LangGraph execution resets checkpoint_ns to an empty string.
        # Isolate runs by thread instead; the same run ID remains resumable.
        thread_id = f"run:{run['id']}"
        config = {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": "",
            },
            "recursion_limit": int(run["request_snapshot"].get("recursion_limit", 30)),
        }
        trace_callback = None
        if self.stream_tools:
            from app.runtime.tracing import RunTrace, RunTraceCallbackHandler
            trace = self.manager.trace
            if isinstance(trace, RunTrace):
                trace_callback = RunTraceCallbackHandler(trace)
                config["callbacks"] = [trace_callback]
        final_updates: dict[str, Any] = {}
        latest_values: dict[str, Any] = {}
        message_parts: list[str] = []
        tool_activities: dict[str, tuple[str, str]] = {}
        stream_kwargs = {
            "config": config, "stream_mode": stream_modes, "version": "v2",
        }
        if context is not None:
            stream_kwargs["context"] = context
        try:
            async for part in self.graph.astream(graph_input, **stream_kwargs):
                if cancel_event.is_set():
                    raise asyncio.CancelledError
                part_type = part.get("type")
                if part_type == "messages" and self.stream_messages:
                    message, metadata = part.get("data", (None, {}))
                    if not self._message_is_public(metadata):
                        continue
                    text = self._message_text(message)
                    if text:
                        message_parts.append(text)
                        await self.manager.publish(
                            run["id"],
                            "messages",
                            {
                                "message_id": f"assistant:{run['id']}",
                                "delta": text,
                            },
                            durable=False,
                        )
                elif part_type == "custom":
                    payload = self._validated_custom(part.get("data"))
                    if payload is not None:
                        await self.manager.publish(
                            run["id"], "custom", payload,
                            durable=payload.get("kind") != "agent.activity.progress",
                        )
                elif part_type == "tools" and self.stream_tools:
                    payload = self._safe_tool_activity(part.get("data"), tool_activities)
                    if payload is not None:
                        await self.manager.publish(
                            run["id"], "custom", payload, durable=True
                        )
                elif part_type == "updates":
                    data = part.get("data") or {}
                    interrupts = data.get("__interrupt__") or ()
                    if interrupts:
                        await self._handle_interrupt(run["id"], interrupts[0])
                        return None
                    for node_name, update in data.items():
                        if not node_name.startswith("__") and isinstance(update, dict):
                            final_updates.update(update)
                elif part_type == "values":
                    data = part.get("data") or {}
                    interrupts = (
                        part.get("interrupts")
                        or (data.get("__interrupt__") if isinstance(data, dict) else ())
                        or ()
                    )
                    if interrupts:
                        await self._handle_interrupt(run["id"], interrupts[0])
                        return None
                    if isinstance(data, dict):
                        latest_values = {
                            key: value
                            for key, value in data.items()
                            if not key.startswith("__")
                        }

            assistant_text = "".join(message_parts)
            if self.stream_tools:
                metrics.observe("main_agent_seconds", time.monotonic() - started_at)
            if self.finalizer:
                return await self.finalizer(
                    run,
                    {**final_updates, **latest_values},
                    assistant_text,
                )
            return None
        finally:
            if trace_callback is not None:
                trace_callback.finish_pending()

    async def _handle_interrupt(self, run_id: str, interrupt_value: Any) -> None:
        value = getattr(interrupt_value, "value", None) or {}
        if not isinstance(value, dict):
            value = {"question": str(value)}
        # LangGraph may reuse its interrupt ID for successive questions in one
        # node. Public reply IDs identify each question, so a later answer must
        # not collide with the exactly-once record of an earlier answer.
        interaction_id = str(uuid.uuid4())
        safe_payload = {
            "kind": "run.waiting_user",
            "interaction_id": interaction_id,
            "question": str(value.get("question") or "请补充所需信息"),
            "missing_fields": [str(item) for item in value.get("missing_fields") or []],
            "input_schema": value.get("input_schema") or {},
        }
        await self.manager.publish(
            run_id, "custom", safe_payload, durable=True
        )
        await self.manager.transition(
            run_id,
            RunStatus.WAITING_USER,
            outstanding_interaction_id=interaction_id,
        )

    def _message_is_public(self, metadata: dict[str, Any]) -> bool:
        node = metadata.get("langgraph_node")
        tags = set(metadata.get("tags") or ())
        return node in self.visible_nodes or bool(tags & self.visible_tags)

    @staticmethod
    def _message_text(message: Any) -> str:
        content = getattr(message, "content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                str(block.get("text", ""))
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
        return ""

    @staticmethod
    def _validated_custom(payload: Any) -> dict[str, Any] | None:
        if not isinstance(payload, dict):
            return None
        try:
            return _custom_adapter.validate_python(payload).model_dump()
        except ValidationError:
            return None

    @staticmethod
    def _safe_tool_activity(
        raw: Any, known: dict[str, tuple[str, str]]
    ) -> dict[str, Any] | None:
        """Project provider-specific tool stream records onto a fixed public shape."""
        if not isinstance(raw, dict):
            return None
        from app.chat.tools import TOOL_PRESENTATION

        event_type = str(raw.get("type") or raw.get("event") or raw.get("status") or "").casefold()
        name = str(raw.get("name") or raw.get("tool_name") or "")
        call_id = str(raw.get("id") or raw.get("tool_call_id") or raw.get("call_id") or "")
        nested = raw.get("tool_call") or raw.get("data")
        if isinstance(nested, dict):
            name = name or str(nested.get("name") or nested.get("tool_name") or "")
            call_id = call_id or str(nested.get("id") or nested.get("tool_call_id") or "")
        if not call_id:
            return None
        if name in TOOL_PRESENTATION:
            known[call_id] = TOOL_PRESENTATION[name]
        presentation = known.get(call_id)
        if presentation is None:
            return None
        failed = bool(raw.get("error")) or event_type in {"error", "failed", "tool_error", "tool-error"}
        completed = event_type in {"tool_result", "result", "end", "ended", "completed", "success", "tool-finished"}
        started = event_type in {"tool_call", "start", "started", "on_tool_start", "tool-started"}
        if failed:
            kind, stage = "agent.activity.failed", "failed"
        elif completed:
            kind, stage = "agent.activity.completed", "completed"
        elif started:
            kind, stage = "agent.activity.started", "started"
        else:
            return None
        activity_type, label = presentation
        return {
            "kind": kind, "activity_id": f"tool:{call_id}",
            "activity_type": activity_type, "label": label,
            "stage": stage, "stats": {},
        }

"""Explicit, content-free Run traces; export is isolated from business execution.

No global OpenTelemetry provider or auto-instrumentation is installed. Only this
module owns spans, so request bodies, headers and exception text never enter them.
"""
from __future__ import annotations

import asyncio
from collections import Counter, OrderedDict
from contextlib import contextmanager, nullcontext
from datetime import datetime
import json
import logging
import os
from functools import wraps
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit

from langchain_core.callbacks import AsyncCallbackHandler

logger = logging.getLogger("app.runtime.trace")
_ACTIVE_TRACE_KEY = "waygent.runtime_trace"
EXPECTED_SPAN_NAMES = (
    "runtime.run", "runtime.queue", "runtime.execution", "runtime.model_call",
    "runtime.tool_call", "runtime.provider_request", "runtime.capacity_wait", "planning.weather_lookup",
    "planning.attraction_search", "planning.candidate_builder", "planning.optimizer", "planning.walking_time_check",
    "planning.quality_gate", "planning.full_revision", "planning.local_reorder", "planning.local_add_remove",
    "planning.local_replace", "planning.meal_search", "planning.meal_recommend",
    "planning.finalize",
)


_SCENARIO_SPANS = {
    "react_model_success": ("runtime.run", "runtime.queue", "runtime.execution", "runtime.model_call"),
    "react_tool_success": ("runtime.run", "runtime.queue", "runtime.execution", "runtime.model_call", "runtime.tool_call"),
    "provider_retry_error": ("runtime.run", "runtime.queue", "runtime.execution", "runtime.model_call"),
    "provider_http_request": ("runtime.run", "runtime.queue", "runtime.execution", "runtime.provider_request"),
    "provider_cache_hit": ("runtime.run", "runtime.queue", "runtime.execution"),
    "new_planning_success": ("runtime.run", "runtime.queue", "runtime.execution", "planning.weather_lookup", "planning.attraction_search", "planning.candidate_builder", "runtime.model_call", "planning.optimizer", "planning.walking_time_check", "planning.quality_gate", "planning.finalize"),
    "revision_success": ("runtime.run", "runtime.queue", "runtime.execution", "runtime.model_call", "runtime.tool_call", "planning.full_revision", "planning.weather_lookup", "planning.attraction_search", "planning.candidate_builder", "planning.optimizer", "planning.walking_time_check", "planning.quality_gate", "planning.finalize"),
    "local_reorder_success": ("runtime.run", "runtime.queue", "runtime.execution", "runtime.model_call", "runtime.tool_call", "planning.local_reorder"),
    "local_add_remove_success": ("runtime.run", "runtime.queue", "runtime.execution", "runtime.model_call", "runtime.tool_call", "planning.local_add_remove"),
    "capacity_wait": ("runtime.run", "runtime.queue", "runtime.execution", "runtime.capacity_wait"),
    "capacity_cancel": ("runtime.run", "runtime.queue", "runtime.execution", "runtime.capacity_wait"),
}

def _set_optional_number(span, attribute, value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return
    try:
        span.set_attribute(attribute, int(value))
    except Exception:
        pass


def _record_usage(span, usage):
    """Copy only provider-reported usage fields; absence is not a zero."""
    if not isinstance(usage, dict):
        return
    for source, target in (("input_tokens", "usage.input_tokens"),
                           ("output_tokens", "usage.output_tokens"),
                           ("total_tokens", "usage.total_tokens")):
        _set_optional_number(span, target, usage.get(source))
    details = usage.get("input_token_details") or usage.get("prompt_tokens_details")
    if not isinstance(details, dict):
        details = {}
    for names, target in (
        (("cache_read", "cache_read_tokens", "cached_tokens"), "usage.cache_read_tokens"),
        (("cache_creation", "cache_creation_tokens", "cache_miss"), "usage.cache_creation_tokens"),
    ):
        for name in names:
            if name in details:
                _set_optional_number(span, target, details[name])
                break


@contextmanager
def trace_stage(name, attributes=None):
    """Create a child span only inside an active Run execution context."""
    try:
        from opentelemetry.context import get_value
        trace = get_value(_ACTIVE_TRACE_KEY)
    except Exception:
        trace = None
    manager = trace.stage(name, attributes=attributes) if isinstance(trace, RunTrace) else nullcontext(None)
    with manager as span:
        yield span


def record_provider_response(span, data):
    """Classify only Amap's fixed response status, never copy payload values."""
    if span is None or not isinstance(data, dict):
        return
    try:
        response_status = data.get("status")
        if response_status not in {"0", "1"}:
            return
        span.set_attribute("provider.outcome", "accepted" if response_status == "1" else "rejected")
        if response_status == "0":
            from opentelemetry.trace import Status, StatusCode
            span.set_status(Status(StatusCode.ERROR))
    except Exception:
        logger.warning("run_trace_observer_failed")


def record_provider_rate_limit_retry(attempt):
    """Record the actual POI retry decision without provider error text."""
    try:
        from opentelemetry.context import get_value
        from opentelemetry.trace import get_current_span
        if isinstance(get_value(_ACTIVE_TRACE_KEY), RunTrace):
            get_current_span().add_event("provider.retry", {
                "provider.kind": "amap", "provider.retry_reason": "rate_limit",
                "provider.retry_attempt": attempt + 1,
            })
    except Exception:
        logger.warning("run_trace_observer_failed")


def observation_only(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except Exception:
            logger.warning("run_trace_observer_failed")
            args[0]._count("observer_failed")
    return guarded


class RunTrace:
    def __init__(self, *, archive_path: Path | str, endpoint: str | None = None,
                 export_timeout: float = 1.0, queue_size: int = 2048):
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.sampling import ALWAYS_ON
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
        from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
        import requests

        if endpoint:
            url = urlsplit(endpoint)
            if (url.scheme != "http" or url.hostname not in {"localhost", "127.0.0.1", "::1", "trace-collector"}
                    or url.username or url.password or url.query or url.fragment or url.path != "/v1/traces"):
                raise ValueError("Run trace endpoint must be a local, credential-free OTLP HTTP /v1/traces URL")
        self._lock = threading.RLock()
        self._counts = Counter()
        self._trace_stats = OrderedDict()
        self._trace_runs = OrderedDict()
        self._runs = {}
        self._ended = OrderedDict()
        self._closed = False
        self._flush_result = None
        self.archive_path = Path(archive_path)
        self.archive_path.parent.mkdir(parents=True, exist_ok=True)
        owner = self

        class Exporter(SpanExporter):
            def export(self, spans):
                owner._count("export_attempted", len(spans))
                owner._trace_count_batch(spans, "export_attempted")
                try:
                    with owner.archive_path.open("a", encoding="utf-8") as archive:
                        for span in spans:
                            archive.write(json.dumps({
                                "type": "span", "name": span.name,
                                "trace_id": f"{span.context.trace_id:032x}",
                                "span_id": f"{span.context.span_id:016x}",
                                "parent_span_id": f"{span.parent.span_id:016x}" if span.parent else None,
                                "start_time_unix_nano": span.start_time,
                                "end_time_unix_nano": span.end_time,
                                "attributes": dict(span.attributes or {}),
                                "events": [{"name": event.name, "timestamp": event.timestamp,
                                            "attributes": dict(event.attributes or {})} for event in span.events],
                                "status": span.status.status_code.name,
                            }, ensure_ascii=False) + "\n")
                    owner._count("archived", len(spans))
                    owner._trace_count_batch(spans, "archived")
                except Exception:
                    owner._count("archive_failed", len(spans))
                    owner._trace_count_batch(spans, "archive_failed")
                    logger.warning('run_trace_archive_failed')
                if not endpoint:
                    self.record_batch(spans, "archive_only")
                    return SpanExportResult.SUCCESS
                try:
                    # One bounded attempt. No SDK exporter retry/backoff or proxy credentials.
                    with requests.Session() as session:
                        session.trust_env = False
                        response = session.post(endpoint, data=encode_spans(spans).SerializeToString(),
                                                headers={"Content-Type": "application/x-protobuf"},
                                                timeout=max(.01, min(export_timeout, 5.0)), allow_redirects=False)
                    if response.status_code != 200:
                        raise RuntimeError("OTLP rejected")
                    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceResponse
                    acknowledgement = ExportTraceServiceResponse.FromString(response.content)
                    rejected = min(len(spans), max(0, acknowledgement.partial_success.rejected_spans))
                    # HTTP accepted != proof that the collector/Jaeger persisted every span.
                    owner._count("http_accepted", len(spans) - rejected)
                    if not rejected:
                        owner._trace_count_batch(spans, "http_accepted")
                    elif len({span.context.trace_id for span in spans}) > 1:
                        owner._count("receiver_ack_run_unattributed", rejected)
                    elif len({span.context.trace_id for span in spans}) == 1:
                        owner._trace_count_batch(spans, "http_accepted", amount_per_span=False, amount=len(spans) - rejected)
                        owner._trace_count_batch(spans, "receiver_rejected", amount_per_span=False, amount=rejected)
                    if rejected:
                        owner._count("export_failed", rejected)
                        owner._count("receiver_rejected", rejected)
                        self.record_batch(spans, "partial_rejected")
                        logger.warning("run_trace_receiver_rejected")
                        return SpanExportResult.FAILURE
                    self.record_batch(spans, "http_accepted")
                    return SpanExportResult.SUCCESS
                except Exception:
                    owner._count("export_failed", len(spans))
                    owner._trace_count_batch(spans, "export_failed")
                    logger.warning('run_trace_export_failed')
                    self.record_batch(spans, "failed")
                    return SpanExportResult.FAILURE

            def record_batch(self, spans, result):
                try:
                    with owner.archive_path.open("a", encoding="utf-8") as archive:
                        archive.write(json.dumps({"type": "export_batch", "result": result,
                            "span_ids": [f"{span.context.span_id:016x}" for span in spans],
                            "trace_ids": sorted({f"{span.context.trace_id:032x}" for span in spans}),
                            "timestamp_unix_nano": time.time_ns()}) + "\n")
                except Exception:
                    owner._count("archive_report_failed")
                    logger.warning("run_trace_archive_report_failed")

        self._provider = TracerProvider(resource=Resource({"service.name": "waygent-runtime"}),
                                        sampler=ALWAYS_ON,
                                        shutdown_on_exit=False)
        self._processor = BatchSpanProcessor(Exporter(), max_queue_size=max(1, queue_size),
                                            max_export_batch_size=min(128, max(1, queue_size)),
                                            schedule_delay_millis=200, export_timeout_millis=1000)
        self._provider.add_span_processor(self._processor)
        self._tracer = self._provider.get_tracer("app.runtime.run", "1")

    def _count(self, key, amount=1):
        with self._lock:
            self._counts[key] += amount

    @property
    def closed(self):
        return self._closed

    def report(self, *, scenario=None, run_id=None):
        with self._lock:
            trace_ids = [trace_id for trace_id, owner_run_id in self._trace_runs.items()
                         if run_id is not None and owner_run_id == run_id]
            if run_id is not None:
                selected = Counter()
                for trace_id in trace_ids:
                    selected.update(self._trace_stats.get(trace_id, {}))
                generated_by_name = {key.removeprefix("generated."): value
                                     for key, value in selected.items()
                                     if key.startswith("generated.")}
                trace_counts = {key: value for key, value in selected.items()
                                if not key.startswith("generated.")}
            else:
                generated_by_name = {key.removeprefix("generated."): value
                                     for key, value in self._counts.items()
                                     if key.startswith("generated.")}
                trace_counts = None
            result = {"scope": "run-stage-v1", "expected_span_names": list(EXPECTED_SPAN_NAMES),
                      "scenario_inventory": {key: list(value) for key, value in _SCENARIO_SPANS.items()},
                      "sampling": "always_on", "sampled": self._counts["generated"],
                      "generated_by_name": generated_by_name, "trace_counts": trace_counts,
                      "scenario_observed": bool(trace_ids) if run_id is not None else None,
                      "receiver_confirmed": None, "receiver_acknowledgement_scope": "per_run_for_single-trace batches; mixed-trace partial rejection is batch-only", "counters": dict(self._counts),
                      "not_export_attempted": max(0, self._counts["generated"] - self._counts["export_attempted"]),
                      "active_runs": len(self._runs)}
            if run_id is not None and trace_ids:
                trace_counts["not_export_attempted"] = max(0, trace_counts.get("generated", 0) - trace_counts.get("export_attempted", 0))
                result["sampled"] = trace_counts.get("generated", 0)
            if scenario in _SCENARIO_SPANS:
                expected = _SCENARIO_SPANS[scenario]
                result["scenario"] = scenario
                result["scenario_expected_span_names"] = list(expected)
                if run_id is not None and trace_ids:
                    result["scenario_missing_generated"] = [name for name in expected
                                                              if not generated_by_name.get(name)]
            return result
    def _trace_bucket(self, trace_id):
        with self._lock:
            counts = self._trace_stats.setdefault(trace_id, Counter())
            self._trace_stats.move_to_end(trace_id)
            while len(self._trace_stats) > 4096:
                expired, _ = self._trace_stats.popitem(last=False)
                self._trace_runs.pop(expired, None)
            return counts

    def _trace_count(self, span, key, amount=1):
        try:
            trace_id = f"{span.context.trace_id:032x}"
            with self._lock:
                self._trace_bucket(trace_id)[key] += amount
        except Exception:
            self._count("observer_failed")

    def _trace_count_batch(self, spans, key, amount_per_span=True, amount=None):
        by_trace = Counter(f"{span.context.trace_id:032x}" for span in spans)
        if not amount_per_span and len(by_trace) != 1:
            return
        for trace_id, count in by_trace.items():
            with self._lock:
                self._trace_bucket(trace_id)[key] += count if amount_per_span else (len(spans) if amount is None else amount)
    def _start(self, name, **kwargs):
        span = self._tracer.start_span(name, **kwargs)
        self._count("generated")
        self._count(f"generated.{name}")
        self._trace_count(span, "generated")
        self._trace_count(span, f"generated.{name}")
        return span
    @contextmanager
    def stage(self, name, *, attributes=None):
        span = None
        token = None
        try:
            clean = {"stage.name": name}
            for key, value in (attributes or {}).items():
                if key in {"model.attempt", "model.outcome", "model.call_count",
                           "model.retry_reason", "tool.name", "tool.outcome", "tool.call_count",
                           "capacity.kind", "capacity.wait_ms", "provider.kind",
                           "provider.attempt", "provider.call_count"} and isinstance(value, (str, int, float, bool)):
                    clean[key] = value
            span = self._start(name, attributes=clean)
            from opentelemetry.context import attach
            from opentelemetry.trace import set_span_in_context
            token = attach(set_span_in_context(span))
        except Exception:
            self._count("observer_failed")
            logger.warning("run_trace_observer_failed")
            span = None
            token = None
        try:
            yield span
        except BaseException:
            if span is not None:
                try:
                    from opentelemetry.trace import Status, StatusCode
                    span.set_status(Status(StatusCode.ERROR))
                except Exception:
                    self._count("observer_failed")
                    logger.warning("run_trace_observer_failed")
            raise
        finally:
            if span is not None:
                try:
                    from opentelemetry.context import detach
                    from opentelemetry.trace import Status, StatusCode
                    if span.status.status_code is StatusCode.UNSET:
                        span.set_status(Status(StatusCode.OK))
                    span.end()
                except Exception:
                    self._count("observer_failed")
                    logger.warning("run_trace_observer_failed")
                finally:
                    if token is not None:
                        try:
                            detach(token)
                        except Exception:
                            self._count("observer_failed")
                            logger.warning("run_trace_observer_failed")
    @staticmethod
    def record_usage(span, usage):
        try:
            _record_usage(span, usage)
        except Exception:
            logger.warning("run_trace_observer_failed")
    @observation_only
    def queued(self, run):
        from opentelemetry.trace import set_span_in_context
        with self._lock:
            if self._closed or run["id"] in self._runs or run["id"] in self._ended:
                return
            # Only opaque IDs and fixed enums are copied; never user IDs or snapshots.
            attributes = {"run.id": run["id"], "run.kind": run["kind"]}
            if run.get("conversation_id"):
                attributes["conversation.id"] = run["conversation_id"]
            queued_at = (int(datetime.fromisoformat(run["queued_at"]).timestamp() * 1_000_000_000)
                         if run["status"] == "queued" else time.time_ns())
            # Explicit empty Context avoids accidentally adopting an unrelated inbound span.
            from opentelemetry.context import Context
            root = self._start("runtime.run", context=Context(), attributes=attributes, start_time=queued_at)
            trace_id = f"{root.get_span_context().trace_id:032x}"
            self._trace_runs[trace_id] = run["id"]
            self._trace_runs.move_to_end(trace_id)
            while len(self._trace_runs) > 4096:
                self._trace_runs.popitem(last=False)
            queue = self._start("runtime.queue", context=set_span_in_context(root), start_time=queued_at)
            self._runs[run["id"]] = {"root": root, "queue": queue, "progress": False, "status": "queued"}
            logger.info(json.dumps({"event": "run_trace_created", "run_id": run["id"],
                                    "trace_id": f"{root.get_span_context().trace_id:032x}"}))

    @observation_only
    def status(self, run):
        with self._lock:
            self.queued(run)
            state = self._runs.get(run["id"])
            if not state:
                return
            status = run["status"]
            state["status"] = status
            state["root"].set_attribute("run.status", status)
            state["root"].add_event("run.status", {"run.status": status})
            if status == "running" and state.get("queue"):
                state.pop("queue").end()
            if status in {"succeeded", "failed", "cancelled"}:
                if state.get("queue"):
                    state.pop("queue").end()
                from opentelemetry.trace import Status, StatusCode
                state["root"].set_status(Status(StatusCode.ERROR if status == "failed" else StatusCode.OK))
                state["root"].end()
                self._runs.pop(run["id"], None)
                self._ended[run["id"]] = None
                if len(self._ended) > 4096:
                    self._ended.popitem(last=False)

    @observation_only
    def progress(self, run_id):
        with self._lock:
            state = self._runs.get(run_id)
            if state and not state["progress"]:
                state["progress"] = True
                state["root"].add_event("run.first_progress")

    @observation_only
    def resume_queued(self, run):
        from opentelemetry.trace import set_span_in_context
        self.queued(run)
        with self._lock:
            state = self._runs.get(run["id"])
            if state and not state.get("queue"):
                state["queue"] = self._start("runtime.queue", context=set_span_in_context(state["root"]),
                                              attributes={"run.resumed": True})

    @contextmanager
    def execution(self, run, *, resumed=False):
        from opentelemetry.context import attach, detach, set_value
        from opentelemetry.trace import set_span_in_context
        span = self._begin_execution(run, resumed=resumed)
        token = None
        if span is not None:
            try:
                context = set_span_in_context(span)
                context = set_value(_ACTIVE_TRACE_KEY, self, context=context)
                token = attach(context)
            except Exception:
                self._count("observer_failed")
                logger.warning("run_trace_observer_failed")
        try:
            yield
        finally:
            # Never swallow or record a business exception. Only observer operations
            # are guarded, and no exception values enter telemetry.
            try:
                if token is not None:
                    detach(token)
                if span is not None:
                    span.end()
            except Exception:
                self._count("observer_failed")
                logger.warning("run_trace_observer_failed")

    @observation_only
    def _begin_execution(self, run, *, resumed=False):
        from opentelemetry.trace import set_span_in_context
        self.queued(run)
        with self._lock:
            state = self._runs.get(run["id"])
            if state and state.get("queue"):
                state.pop("queue").end()
            if state:
                state["status"] = "running"
                state["root"].set_attribute("run.status", "running")
            span = self._start("runtime.execution", context=set_span_in_context(state["root"]),
                               attributes={"run.id": run["id"], "run.resumed": resumed}) if state else None
        return span

    async def close(self, timeout_ms=2000):
        with self._lock:
            if self._closed:
                return bool(self._flush_result)
            self._closed = True
            for state in self._runs.values():
                if state.get("queue"):
                    state["queue"].end()
                state["root"].set_attribute("run.status", state["status"])
                state["root"].add_event("runtime.shutdown")
                state["root"].end()
            self._runs.clear()
        # Flush/shutdown only in a daemon thread, never on the application exit path.
        # requests' timeout limits connect/read inactivity, not total HTTP duration;
        # the async deadline below bounds the app's wait even if export stays stuck.
        # A timed-out flush is never claimed to have drained successfully.
        timeout_ms = max(1, min(timeout_ms, 10000))
        finished = threading.Event()
        result = []
        def flush_and_shutdown():
            try:
                result.append(self._processor.force_flush(timeout_ms))
            except Exception:
                result.append(False)
            finally:
                finished.set()
                self._provider.shutdown()
        threading.Thread(target=flush_and_shutdown, name="run-trace-shutdown", daemon=True).start()
        deadline = time.monotonic() + timeout_ms / 1000
        while not finished.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(min(.01, max(0, deadline - time.monotonic())))
        drained = finished.is_set() and bool(result and result[0])
        self._count("flush_completed" if drained else "flush_timed_out")
        self._flush_result = bool(drained)
        if not drained:
            logger.warning("run_trace_flush_timed_out")
        return bool(drained)


class RunTraceCallbackHandler(AsyncCallbackHandler):
    """Capture ReAct model/tool lifecycle without copying callback payloads."""
    ALLOWED_TOOLS = {
        "get_travel_memory", "get_planning_context", "find_saved_itineraries",
        "get_saved_itinerary", "update_current_brief", "submit_current_brief",
        "start_revision", "control_run",
    }

    def __init__(self, trace):
        from threading import RLock
        self.trace = trace
        self._lock = RLock()
        self._spans = {}

    def _begin(self, run_id, name, attributes):
        try:
            key = str(run_id)
            with self._lock:
                if key in self._spans:
                    return
            span = self.trace._start(name, attributes=attributes)
            with self._lock:
                self._spans[key] = span
        except Exception:
            self.trace._count("observer_failed")
            logger.warning("run_trace_observer_failed")

    def _finish(self, run_id, outcome, usage=None):
        try:
            with self._lock:
                span = self._spans.pop(str(run_id), None)
            if span is None:
                return
            from opentelemetry.trace import Status, StatusCode
            if usage is not None:
                _record_usage(span, usage)
            span.set_attribute("stage.outcome", outcome)
            span.set_status(Status(StatusCode.ERROR if outcome == "error" else StatusCode.OK))
            span.end()
        except Exception:
            self.trace._count("observer_failed")
            logger.warning("run_trace_observer_failed")

    def finish_pending(self):
        """End callbacks whose framework lifecycle was interrupted by cancellation."""
        try:
            with self._lock:
                run_ids = tuple(self._spans)
            for run_id in run_ids:
                self._finish(run_id, "error")
        except Exception:
            self.trace._count("observer_failed")
            logger.warning("run_trace_observer_failed")

    async def on_chat_model_start(self, _serialized, _messages, *, run_id, **_kwargs):
        self._begin(run_id, "runtime.model_call", {"stage.name": "runtime.model_call", "model.call_count": 1})

    async def on_llm_start(self, _serialized, _prompts, *, run_id, **_kwargs):
        self._begin(run_id, "runtime.model_call", {"stage.name": "runtime.model_call", "model.call_count": 1})

    async def on_llm_end(self, response, *, run_id, **_kwargs):
        usage = None
        try:
            usage = getattr(response, "llm_output", None) or {}
            usage = usage.get("token_usage") if isinstance(usage, dict) else None
            if isinstance(usage, dict):
                original = usage
                usage = {"input_tokens": original.get("prompt_tokens"),
                         "output_tokens": original.get("completion_tokens"),
                         "total_tokens": original.get("total_tokens")}
                details = original.get("prompt_tokens_details")
                if isinstance(details, dict):
                    usage["input_token_details"] = {
                        "cache_read": details["cached_tokens"]
                    } if "cached_tokens" in details else {}
            for group in getattr(response, "generations", ()) or ():
                for generation in group or ():
                    message = getattr(generation, "message", None)
                    metadata = getattr(message, "usage_metadata", None)
                    if isinstance(metadata, dict):
                        usage = metadata
                        break
                if isinstance(usage, dict):
                    break
        except Exception:
            usage = None
        self._finish(run_id, "success", usage if isinstance(usage, dict) else None)

    async def on_llm_error(self, _error, *, run_id, **_kwargs):
        self._finish(run_id, "error")

    async def on_tool_start(self, serialized, _input_str, *, run_id, **_kwargs):
        name = serialized.get("name") if isinstance(serialized, dict) else None
        attrs = {"stage.name": "runtime.tool_call", "tool.call_count": 1}
        if name in self.ALLOWED_TOOLS:
            attrs["tool.name"] = name
        self._begin(run_id, "runtime.tool_call", attrs)

    async def on_tool_end(self, _output, *, run_id, **_kwargs):
        self._finish(run_id, "success")

    async def on_tool_error(self, _error, *, run_id, **_kwargs):
        self._finish(run_id, "error")

    async def on_retry(self, _retry_state, *, run_id, **_kwargs):
        try:
            with self._lock:
                span = self._spans.get(str(run_id))
            if span is not None:
                span.add_event("model.retry")
        except Exception:
            self.trace._count("observer_failed")
            logger.warning("run_trace_observer_failed")

class NoRunTrace:
    closed = False
    def queued(self, run): pass
    def status(self, run): pass
    def progress(self, run_id): pass
    def resume_queued(self, run): pass
    @contextmanager
    def execution(self, run, *, resumed=False): yield
    async def close(self, timeout_ms=2000):
        self.closed = True
        return True
    def report(self): return {"enabled": False}


def configured_run_trace():
    if os.getenv("RUNTIME_TRACE_ENABLED", "false").lower() != "true":
        return NoRunTrace()
    try:
        return RunTrace(archive_path=os.getenv("RUNTIME_TRACE_ARCHIVE", ".local/evidence/runtime-traces/spans.jsonl"),
                        endpoint=os.getenv("RUNTIME_TRACE_OTLP_ENDPOINT") or None,
                        export_timeout=float(os.getenv("RUNTIME_TRACE_EXPORT_TIMEOUT_SECONDS", "1")))
    except Exception:
        logger.warning("run_trace_configuration_failed")
        return NoRunTrace()

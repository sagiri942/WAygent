"""Application service for persistent LLM-understood travel conversations."""

from __future__ import annotations

from app.planning.revision_timeout import (
    DEFAULT_PLANNING_TASK_TIMEOUT_SECONDS,
    revision_execution,
    run_with_planning_budget,
)

import asyncio
import json
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.chat.models import MainAgentContext
from app.chat.artifacts import validate_message_artifacts
from app.chat.prompts import main_agent_messages
from app.core.itinerary_storage import itinerary_connection
from app.core.memory import (ItineraryVersionConflict, get_itinerary_record, load_itinerary, save_itinerary)
from app.chat.memory_service import ChatMemoryService
from app.chat.planning_memory import PlanningMemoryMatcher
from app.core.planning_constraints import compatibility_preferences, constraints_for_prompt, planning_instruction
from app.core.planning_brief import required_brief_fields
from app.planning.full_revision import supersedes_existing_constraint
from app.core.travel_memory import ConversationMemoryRepository
from app.runtime.manager import RunManager
from app.runtime.models import RunKind, concurrency_key
from app.runtime.repositories import (
    ConversationRunActive,
    ConversationRepository,
    OwnedResourceNotFound,
    PlanningBriefRepository,
    RunRepository,
)
from app.runtime.observability import metrics


class ChatService:
    def __init__(self, manager: RunManager, db_path: str | Path | None = None):
        self.manager = manager
        self.conversations = ConversationRepository(db_path)
        self.briefs = PlanningBriefRepository(db_path)
        self.runs = RunRepository(db_path)
        self.memory_context = ChatMemoryService(db_path)
        self.planning_memory = PlanningMemoryMatcher(db_path)
        # Local import avoids a service/executor import cycle during module loading.
        from app.chat.executor import DialogueActionExecutor

        self.actions = DialogueActionExecutor(self)

    async def submit_message(
        self,
        user_id: str,
        conversation_id: str,
        content: str,
        *,
        related_run_id: str | None = None,
        related_itinerary_id: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Persist a message and queue understanding; never infer text semantics here."""
        self.memory_context.validate_message(content)
        if related_run_id:
            await asyncio.to_thread(self.runs.get, user_id, related_run_id)
        if related_itinerary_id:
            await asyncio.to_thread(
                self._owned_itinerary_summary, user_id, related_itinerary_id
            )
        message, run = await asyncio.to_thread(
            self.conversations.add_user_message_and_run,
            user_id,
            conversation_id,
            content,
            related_run_id=related_run_id,
            related_itinerary_id=related_itinerary_id,
        )
        return message, run

    async def submit_brief(
        self, user_id: str, brief_id: str, *, origin_chat_run_id: str | None = None
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        current = await asyncio.to_thread(self.briefs.get, user_id, brief_id)
        missing = required_brief_fields(current.get("data") or {})
        if missing:
            raise ValueError("planning brief is not ready: " + ",".join(missing))
        if current["status"] in self.briefs.ACTIVE:
            current = await self.planning_memory.refresh(user_id, brief_id)

        def create_run(conn, snapshot, conversation_id):
            if getattr(getattr(conn, "dialect", None), "name", None) == "mysql":
                from sqlalchemy import select
                from app.core.identity_schema import runs as run_table
                from app.core.mysql_travel_memory import CoreConversationMemoryRepository
                active_query = select(run_table).where(
                    run_table.c.conversation_id == conversation_id,
                    run_table.c.user_id == user_id,
                    run_table.c.kind.in_(["chat", "travel_plan", "revision"]),
                    run_table.c.status.in_(["queued", "running", "waiting_user"]),
                )
                if origin_chat_run_id:
                    active_query = active_query.where(run_table.c.id != origin_chat_run_id)
                active = conn.execute(active_query.with_for_update()).mappings().first()
                if active:
                    from app.runtime.mysql_repositories import _decode_run
                    raise ConversationRunActive(_decode_run(active))
                run_id = str(uuid.uuid4())
                request = dict(snapshot)
                if origin_chat_run_id:
                    request["origin_chat_run_id"] = origin_chat_run_id
                request.update(compatibility_preferences(request.get("effective_constraints") or []))
                request.setdefault("query", self._snapshot_query(request))
                request["planning_instruction"] = planning_instruction(request)
                memory = CoreConversationMemoryRepository(self.manager.database).ensure_snapshot(
                    user_id, conversation_id, conn
                )
                request["memory_profile_revision"] = memory["profile_revision"]
                request["memory_profile_snapshot"] = memory["profile_snapshot"]
                return self.runs.insert(
                    conn, run_id=run_id, user_id=user_id, kind=RunKind.TRAVEL_PLAN,
                    concurrency_key=concurrency_key(RunKind.TRAVEL_PLAN, run_id=run_id),
                    request_snapshot=request, conversation_id=conversation_id,
                )
            active = conn.execute(
                "SELECT * FROM runs WHERE conversation_id=? AND user_id=? "
                "AND kind IN ('chat','travel_plan','revision') "
                "AND status IN ('queued','running','waiting_user') "
                "AND (? IS NULL OR id<>?) ORDER BY created_at DESC,id DESC LIMIT 1",
                (conversation_id, user_id, origin_chat_run_id, origin_chat_run_id),
            ).fetchone()
            if active:
                raise ConversationRunActive(dict(active))
            run_id = str(uuid.uuid4())
            request = dict(snapshot)
            if origin_chat_run_id:
                request["origin_chat_run_id"] = origin_chat_run_id
            request.update(compatibility_preferences(request.get("effective_constraints") or []))
            request.setdefault("query", self._snapshot_query(request))
            request["planning_instruction"] = planning_instruction(request)
            memory = ConversationMemoryRepository(self.manager.db_path).ensure_snapshot(
                user_id, conversation_id, conn
            )
            request["memory_profile_revision"] = memory["profile_revision"]
            request["memory_profile_snapshot"] = memory["profile_snapshot"]
            return self.runs.insert(
                conn,
                run_id=run_id,
                user_id=user_id,
                kind=RunKind.TRAVEL_PLAN,
                concurrency_key=concurrency_key(RunKind.TRAVEL_PLAN, run_id=run_id),
                request_snapshot=request,
                conversation_id=conversation_id,
            )

        return await asyncio.to_thread(
            self.briefs.submit, user_id, brief_id, create_run
        )

    async def execute_planner_tool(
        self, run: dict[str, Any], brief_id: str
    ) -> tuple[dict[str, Any], str]:
        """Run Planner as an eventful task tool inside the top-level Chat Run."""
        planning_started = time.monotonic()
        current = await asyncio.to_thread(self.briefs.get, run["user_id"], brief_id)
        missing = required_brief_fields(current.get("data") or {})
        if missing:
            raise ValueError("planning brief is not ready: " + ",".join(missing))
        if current["status"] in self.briefs.ACTIVE:
            current = await self.planning_memory.refresh(run["user_id"], brief_id)
            submitted, _same_run = await asyncio.to_thread(
                self.briefs.freeze_for_run, run["user_id"], brief_id, run["id"]
            )
        elif current["status"] == "submitted":
            # A retried Chat Run reuses the immutable submission snapshot. The
            # Planner remains an internal task and must not create another Run.
            submitted = current
        else:
            raise ValueError("planning brief is not ready")
        await self.manager.publish(
            run["id"], "custom", {
                "kind": "planning_brief.submitted",
                "brief_id": submitted["id"],
                "status": "submitted",
                "summary": submitted["data"],
                "missing_fields": [],
            }, durable=True,
        )
        request = dict(submitted.get("submission_snapshot") or submitted["data"])
        request.update(compatibility_preferences(request.get("effective_constraints") or []))
        request.setdefault("query", self._snapshot_query(request))
        request["transport_enabled"] = True
        request["planning_instruction"] = planning_instruction(request)
        memory = await asyncio.to_thread(
            self.memory_context.memories.get, run["user_id"], run["conversation_id"]
        )
        request["memory_profile_revision"] = memory["profile_revision"]
        request["memory_profile_snapshot"] = memory["profile_snapshot"]
        task_run = {**run, "request_snapshot": request}

        from app.planning.graph import build_graph
        from app.planning.runtime_worker import PlanningFinalizer, planning_run_to_state

        graph = build_graph(memory_writer=None, interrupt_on_missing=False)
        state = await planning_run_to_state(task_run)
        latest: dict[str, Any] = {}
        async def collect_graph():
            async for part in graph.astream(
                state, stream_mode=["custom", "values"], version="v2",
                config={
                    "configurable": {
                        "thread_id": str(run["conversation_id"]),
                        "checkpoint_ns": f"run:{run['id']}:planner",
                    }
                },
            ):
                if part.get("type") == "custom" and isinstance(part.get("data"), dict):
                    await self.manager.publish(run["id"], "custom", part["data"], durable=True)
                elif part.get("type") == "values":
                    values = self._graph_values(part.get("data"))
                    if values:
                        latest.update({key: value for key, value in values.items() if not key.startswith("__")})
        remaining_budget = max(
            0.0, DEFAULT_PLANNING_TASK_TIMEOUT_SECONDS - (time.monotonic() - planning_started)
        )
        await run_with_planning_budget(collect_graph(), seconds=remaining_budget)
        if time.monotonic() - planning_started >= DEFAULT_PLANNING_TASK_TIMEOUT_SECONDS:
            from app.planning.revision_timeout import PlanningExecutionTimeout
            raise PlanningExecutionTimeout()
        result = await PlanningFinalizer(self.manager, tips_as_run=False)(task_run, latest, "") or {}
        itinerary_id = str(result.get("result_itinerary_id") or "")
        if not itinerary_id:
            raise RuntimeError("planner task completed without itinerary")
        submitted["walking_repair_summary"] = list(latest.get("walking_repair_summary") or [])
        return submitted, itinerary_id

    @revision_execution
    async def execute_full_revision_tool(
        self, run: dict[str, Any], itinerary_id: str, modification_notes: str, intent, *, transport_budget: str | None = None
    ) -> tuple[str, dict[str, Any]]:
        """Replan a confirmed whole-trip change through the shared deterministic pipeline."""
        planning_started = time.monotonic()
        from copy import deepcopy
        from datetime import date, timedelta
        from app.planning.schemas import TravelPlanState
        from app.core.itinerary_storage import itinerary_connection
        from app.core.memory import get_itinerary_record, load_itinerary
        from app.runtime.tracing import trace_stage

        def load_base():
            with itinerary_connection(self.manager.db_path) as conn:
                row = get_itinerary_record(itinerary_id, run["user_id"], conn)
                saved = load_itinerary(itinerary_id, conn)
            if not row or not saved:
                raise OwnedResourceNotFound("base itinerary not found")
            if not saved.get("planner_state"):
                raise ValueError("revision_base_unverified")
            return row, saved

        row, saved = await asyncio.to_thread(load_base)
        base_state = deepcopy(saved["planner_state"])
        if transport_budget is not None:
            base_state["transport_budget"] = transport_budget
        from app.core.planning_brief import required_brief_fields
        if {"transport_budget", "trip_budget"} & set(required_brief_fields(base_state)):
            from app.planning.local_reorder import LocalReorderRejected
            raise LocalReorderRejected("transport_budget_required", "请先补充有效的景点间交通预算（计入总预算）；原行程保持不变。")
        old_start = base_state.get("travel_start_date") or row.get("start_date") or saved["plan"].get("start_date")
        old_end = base_state.get("travel_end_date") or row.get("end_date") or saved["plan"].get("end_date")
        if isinstance(old_start, str):
            old_start = date.fromisoformat(old_start)
        if isinstance(old_end, str):
            old_end = date.fromisoformat(old_end)
        start = intent.start_date or old_start
        if start is None:
            raise ValueError("revision_base_unverified")
        if intent.end_date:
            end = intent.end_date
        elif intent.start_date and old_start and old_end:
            end = start + (old_end - old_start)
        else:
            end = old_end or start + timedelta(days=max(0, int(base_state.get("days") or 1) - 1))
        days = (end - start).days + 1
        if days < 1 or days > 14:
            raise ValueError("revision_date_range_unsupported")
        destination = intent.destination or base_state.get("destination") or saved["plan"].get("destination")
        if not destination:
            raise ValueError("revision_base_unverified")
        old_constraints = list(base_state.get("effective_constraints") or [])
        constraints = [
            item for item in old_constraints
            if not supersedes_existing_constraint(item, intent)
        ]
        existing_ids = {str(item.get("id") or "") for item in constraints}
        for item in intent.constraints:
            candidate = dict(item)
            base_id = str(candidate.get("id") or "full-revision-constraint")
            candidate_id = base_id
            suffix = 2
            while candidate_id in existing_ids:
                candidate_id = f"{base_id}-revision-{suffix}"
                suffix += 1
            candidate["id"] = candidate_id
            existing_ids.add(candidate_id)
            constraints.append(candidate)
        route_summary = "；".join(
            f"第{int(day.get('day') or index + 1)}天：" + "、".join(
                str(spot.get("name") or "") for spot in day.get("spots") or []
            )
            for index, day in enumerate(base_state.get("route") or [])
        )
        from app.planning.transport_check import new_transport_query
        snapshot = {
            **base_state,
            # Preferences and prompt hints are projections of the revised
            # constraints; copying the base can resurrect a superseded limit.
            **compatibility_preferences(constraints),
            "profile_hint": constraints_for_prompt(constraints),
            "query": f"{destination}{days}日游；基于原行程进行全量修改：{intent.summary}",
            "planning_instruction": (
                f"保留原规划需求中未被明确修改的要求。用户已确认的全量修改：{intent.summary}。"
                "这些结构化约束必须完整进入候选筛选、确定性求解和质量门。"
            ),
            "destination": destination,
            "travel_start_date": start,
            "travel_end_date": end,
            "days": days,
            "effective_constraints": constraints,
            "constraint_coverage": [],
            "route": [],
            "pois": [],
            "candidate_pool": [],
            "candidate_pool_fingerprint": None,
            "candidate_pool_frozen": False,
            "candidate_builder_warnings": [],
            "candidate_repair_round": 0,
            "candidate_repair_feedback": [],
            "solver_diagnostics": None,
            "score_breakdown": [],
            "quality_report": None,
            "walking_time_verification": None,
            "transport_enabled": True,
            **new_transport_query(),
            "walking_time_minutes": {},
            "walking_repair_round": 0,
            "walking_repair_original_route": [],
            "walking_repair_summary": [],
            "approved": False,
            "weather_forecast": [],
            "weather_note": None,
            "meal_candidates": [],
            "meals": [],
            "modification_notes": modification_notes,
            "parent_plan_id": itinerary_id,
            "final_plan": None,
            "planner_reviewer_dialogue": [],
            "route_modify_opinion": None,
            "modification_concern": None,
            "review_round": 0,
            "time_violations": [],
            "time_check_round": 0,
            "time_check_done": False,
            "previous_plan_summary": route_summary,
            "revision_scope": {"kind": "full_revision", "summary": intent.summary},
            "history": list(base_state.get("history") or []) + [
                f"确认的全量修改变更集：{intent.summary}；原行程：{route_summary}"
            ],
        }
        task_run = {**run, "request_snapshot": snapshot}
        from app.planning.graph import build_graph
        from app.planning.runtime_worker import PlanningFinalizer
        from app.providers.amap.walking import query_walking_minutes

        with trace_stage("planning.full_revision") as span:
            try:
                graph = build_graph(
                    memory_writer=None, interrupt_on_missing=False,
                    walking_leg_provider=query_walking_minutes,
                )
                state = TravelPlanState(**snapshot)
                latest: dict[str, Any] = {}
                async def collect_graph():
                    async for part in graph.astream(
                        state, stream_mode=["custom", "values"], version="v2",
                        config={"configurable": {
                            "thread_id": str(run.get("conversation_id") or run["id"]),
                            "checkpoint_ns": f"run:{run['id']}:full-revision",
                        }},
                    ):
                        if part.get("type") == "custom" and isinstance(part.get("data"), dict):
                            await self.manager.publish(run["id"], "custom", part["data"], durable=True)
                        elif part.get("type") == "values":
                            values = self._graph_values(part.get("data"))
                            if values:
                                latest.update({key: value for key, value in values.items() if not key.startswith("__")})
                remaining_budget = max(
                    0.0, DEFAULT_PLANNING_TASK_TIMEOUT_SECONDS - (time.monotonic() - planning_started)
                )
                await run_with_planning_budget(collect_graph(), seconds=remaining_budget)
                if time.monotonic() - planning_started >= DEFAULT_PLANNING_TASK_TIMEOUT_SECONDS:
                    from app.planning.revision_timeout import PlanningExecutionTimeout
                    raise PlanningExecutionTimeout()
                result = await PlanningFinalizer(
                    self.manager, tips_as_run=False,
                    expected_parent_lock_version=int(row.get("lock_version") or 1),
                    require_latest_parent=True, db_path=self.manager.db_path,
                )(task_run, latest, "") or {}
            except Exception as exc:
                if span is not None:
                    try:
                        code = getattr(exc, "code", None) or type(exc).__name__
                        span.set_attribute("revision.failure_code", str(code)[:80])
                    except Exception:
                        pass
                raise
        new_id = str(result.get("result_itinerary_id") or "")
        if not new_id:
            raise RuntimeError("full revision completed without a child itinerary")
        verification = dict(latest.get("walking_time_verification") or {})
        verification["repair_summary"] = list(latest.get("walking_repair_summary") or [])
        return new_id, verification

    async def execute_day_reorder_tool(
        self, run: dict[str, Any], itinerary_id: str, target_day: int,
        modification_notes: str,
    ) -> tuple[str, dict[str, Any]]:
        return await self._execute_local_day_tool(run, itinerary_id, target_day, modification_notes)

    async def execute_day_set_change_tool(
        self, run: dict[str, Any], itinerary_id: str, target_day: int,
        modification_notes: str,
    ) -> tuple[str, dict[str, Any]]:
        return await self._execute_local_day_tool(run, itinerary_id, target_day, modification_notes, set_change=True)

    async def execute_day_replace_tool(
        self, run: dict[str, Any], itinerary_id: str, target_day: int,
        modification_notes: str,
    ) -> tuple[str, dict[str, Any]]:
        return await self._execute_local_day_tool(run, itinerary_id, target_day, modification_notes, replacement=True)

    @revision_execution
    async def _execute_local_day_tool(
        self,
        run: dict[str, Any],
        itinerary_id: str,
        target_day: int,
        modification_notes: str,
        *, set_change: bool = False, replacement: bool = False,
    ) -> tuple[str, dict[str, Any]]:
        """Reorder one saved day without invoking the full planner or enrichment."""
        from app.planning.local_reorder import (
            LocalReorderRejected,
            reorder_single_day,
        )
        from app.providers.amap.walking import query_walking_minutes
        from app.runtime.tracing import trace_stage

        def load_base():
            from app.planning.restaurant_enrichment import (
                load_restaurant_enrichment, route_fingerprint,
            )
            from app.planning.tip_enrichment import load_tip_enrichment

            with itinerary_connection(self.manager.db_path) as conn:
                row = get_itinerary_record(itinerary_id, run["user_id"], conn)
                saved = load_itinerary(itinerary_id, conn)
                tips = restaurant = None
                if saved:
                    fingerprint = route_fingerprint(saved["plan"])
                    tips = load_tip_enrichment(itinerary_id, run["user_id"], fingerprint, conn)
                    restaurant = load_restaurant_enrichment(itinerary_id, run["user_id"], fingerprint, conn)
            if not row or not saved:
                raise OwnedResourceNotFound("base itinerary not found")
            if not saved.get("planner_state"):
                raise LocalReorderRejected(
                    "local_reorder_unverified",
                    "基础行程没有可复用的冻结规划数据，无法安全重排；原版本保持不变。",
                )
            if not saved["planner_state"].get("travel_start_date"):
                saved["planner_state"]["travel_start_date"] = (
                    row.get("start_date") or saved["plan"].get("start_date")
                )
            return row, saved, tips, restaurant

        row, saved, base_tips, base_restaurant = await asyncio.to_thread(load_base)
        solver = reorder_single_day
        if set_change:
            from app.planning.local_add_remove import change_single_day_set
            solver = change_single_day_set
        elif replacement:
            from app.planning.local_add_remove import replace_single_day_attraction
            solver = replace_single_day_attraction
        stage_name = "planning.local_replace" if replacement else ("planning.local_add_remove" if set_change else "planning.local_reorder")
        with trace_stage(stage_name) as span:
            try:
                async def solve_and_verify():
                    provider = query_walking_minutes
                    if saved["planner_state"].get("transport_enabled"):
                        from app.core.planning_brief import required_brief_fields
                        if {"transport_budget", "trip_budget"} & set(required_brief_fields(saved["planner_state"])):
                            raise LocalReorderRejected("transport_budget_required", "请先补充交通预算，原行程保持不变。")
                        from app.core.transport import allowed_transport_modes
                        from app.planning.transport_check import transport_draft_leg
                        from app.planning.local_transport import validate_local_transport
                        modes = allowed_transport_modes(saved["planner_state"].get("effective_constraints", []), saved["planner_state"].get("allowed_transport_modes"))
                        async def provider(origin, destination):
                            return transport_draft_leg(origin, destination, modes)
                    value = await solver(
                        saved["plan"], saved["planner_state"], target_day=target_day,
                        modification_notes=modification_notes, provider=provider,
                    )
                    if saved["planner_state"].get("transport_enabled"):
                        value = await validate_local_transport(value, target_day, reuse_snapshot=True)
                    return value
                reordered = await run_with_planning_budget(solve_and_verify())
            except LocalReorderRejected as exc:
                if span is not None:
                    try:
                        span.set_attribute("revision.failure_code", exc.code)
                    except Exception:
                        pass
                raise

        checkpoint = dict(reordered.planner_state)
        checkpoint["parent_plan_id"] = itinerary_id
        checkpoint["modification_notes"] = modification_notes
        checkpoint.pop("final_plan", None)

        def persist():
            with itinerary_connection(self.manager.db_path) as conn:
                try:
                    new_id = save_itinerary(
                        run["user_id"], reordered.plan, str(row.get("query") or ""),
                        conn, parent_id=itinerary_id,
                        modification_notes=modification_notes,
                        planner_state=checkpoint,
                        expected_parent_lock_version=int(row.get("lock_version") or 1),
                        require_latest_parent=True,
                    )
                    from app.planning.restaurant_enrichment import (
                        route_fingerprint, save_restaurant_enrichment,
                    )
                    from app.planning.tip_enrichment import save_tip_enrichment

                    fingerprint = route_fingerprint(reordered.plan)
                    untouched_names = {
                        str(item.get("name") or "")
                        for day in reordered.plan.get("days") or []
                        if int(day.get("day") or 0) != target_day
                        for item in day.get("timeline") or []
                        if item.get("type") == "attraction"
                    }
                    tip_values = (base_tips or {}).get("tips") or {}
                    preserved_tips = {
                        name: value for name, value in tip_values.items()
                        if name in untouched_names
                    }
                    if preserved_tips:
                        save_tip_enrichment(
                            new_id, run["user_id"], fingerprint, preserved_tips, conn
                        )
                    if base_restaurant:
                        payload = dict(base_restaurant)
                        payload["route_fingerprint"] = fingerprint
                        payload["days"] = [
                            dict(day) for day in payload.get("days") or []
                            if int(day.get("day") or 0) != target_day
                        ]
                        save_restaurant_enrichment(new_id, run["user_id"], payload, conn)
                    return new_id
                except ItineraryVersionConflict as exc:
                    raise LocalReorderRejected(
                        "revision_conflict",
                        "行程基准版本已变化或已有新版本，请刷新后再重试；原版本没有被覆盖。",
                    ) from exc
                except Exception as exc:
                    import sqlite3
                    from sqlalchemy.exc import DBAPIError
                    if isinstance(exc, (DBAPIError, sqlite3.DatabaseError)):
                        raise LocalReorderRejected(
                            "revision_system_error",
                            "行程修改暂时无法保存，原行程保持不变；请稍后重试。",
                        ) from exc
                    raise

        new_itinerary_id = await self.manager.persist_revision(run["id"], persist)
        await self.manager.publish(
            run["id"], "custom",
            {
                "kind": "planning.itinerary_created",
                "itinerary_id": new_itinerary_id,
                "destination": str(reordered.plan.get("destination") or ""),
                "revision_scope": reordered.plan["revision_scope"],
            },
            durable=True,
        )
        if run.get("conversation_id"):
            destination = str(reordered.plan.get("destination") or "这趟旅行")
            message = await asyncio.to_thread(
                ConversationRepository(self.manager.db_path).add_message,
                run["user_id"], run["conversation_id"], "assistant",
                f"{destination}第{target_day}天的路线已核验并保存为新版本，其他日期保持原样。",
                related_run_id=run["id"], related_itinerary_id=new_itinerary_id,
            )
            await self.manager.publish(
                run["id"], "custom", {
                    "kind": "chat.message.completed",
                    "message_id": message["id"],
                    "content": message["content"],
                    "sequence": message["sequence"],
                    "created_at": message["created_at"],
                    "artifacts": message["artifacts"],
                    "related_itinerary_id": new_itinerary_id,
                }, durable=True, validate_custom=False,
            )
        return new_itinerary_id, reordered.verification

    async def apply_brief_patch(
        self,
        run: dict[str, Any],
        patch: dict[str, Any],
    ) -> dict[str, Any]:
        brief = await asyncio.to_thread(
            self.briefs.upsert_active,
            run["user_id"],
            run["conversation_id"],
            patch,
        )
        brief = await self.planning_memory.refresh(run["user_id"], brief["id"])
        event_kind = (
            "planning_brief.ready"
            if brief["status"] == "ready"
            else "planning_brief.updated"
        )
        await self.manager.publish(
            run["id"],
            "custom",
            {
                "kind": event_kind,
                "brief_id": brief["id"],
                "status": brief["status"],
                "summary": brief["data"],
                "missing_fields": brief["missing_fields"],
                "memory_context": brief["memory_context"],
                "effective_constraints": brief["effective_constraints"],
                "constraint_coverage": brief["constraint_coverage"],
            },
            durable=True,
        )
        return brief

    async def update_brief(
        self, user_id: str, brief_id: str, patch: dict[str, Any]
    ) -> dict[str, Any]:
        current = await asyncio.to_thread(self.briefs.get, user_id, brief_id)
        brief = await asyncio.to_thread(
            self.briefs.upsert_active,
            user_id,
            current["conversation_id"],
            patch,
        )
        return await self.planning_memory.refresh(user_id, brief["id"])

    async def refresh_brief_memory(
        self, user_id: str, brief_id: str
    ) -> dict[str, Any]:
        return await self.planning_memory.refresh(user_id, brief_id)

    async def publish_assistant_message(
        self,
        run: dict[str, Any],
        content: str,
        *,
        artifacts: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        message = await asyncio.to_thread(
            self.conversations.add_message,
            run["user_id"],
            run["conversation_id"],
            "assistant",
            content,
            related_run_id=run["id"],
            artifacts=artifacts,
        )
        await self.manager.publish(
            run["id"],
            "custom",
            {
                "kind": "chat.message.completed",
                "message_id": message["id"],
                "content": message["content"],
                "sequence": message["sequence"],
                "created_at": message["created_at"],
                "artifacts": message["artifacts"],
            },
            durable=True,
            validate_custom=False,
        )
        return message

    @staticmethod
    def _graph_values(value: Any) -> dict[str, Any]:
        """Normalize LangGraph values for dict and Pydantic state schemas."""
        if isinstance(value, dict):
            return value
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            dumped = model_dump()
            return dumped if isinstance(dumped, dict) else {}
        return {}

    async def react_chat_input(self, run: dict[str, Any]) -> dict[str, Any]:
        """Rebuild one ReAct turn from the database without dynamic app state."""
        context = await self.memory_context.prepare(
            run,
            application_state={
                "today": datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat(),
                "timezone": "Asia/Shanghai",
            },
        )
        return {"messages": main_agent_messages(context)}

    async def main_agent_context(self, run: dict[str, Any]) -> MainAgentContext:
        memory = await asyncio.to_thread(
            self.memory_context.memories.get,
            run["user_id"],
            run["conversation_id"],
        )
        snapshot = run["request_snapshot"]
        return MainAgentContext(
            user_id=run["user_id"],
            conversation_id=run["conversation_id"],
            chat_run_id=run["id"],
            memory_revision=int(memory["profile_revision"]),
            current_message=str(snapshot.get("text") or ""),
            related_run_id=snapshot.get("related_run_id"),
            related_itinerary_id=snapshot.get("related_itinerary_id"),
        )

    async def finalize_react_chat(
        self,
        run: dict[str, Any],
        updates: dict[str, Any],
        assistant_text: str,
    ) -> dict[str, Any]:
        """Persist the final model answer together with artifacts from tool messages."""
        messages = list(updates.get("messages") or [])
        current_start = max(
            (index for index, message in enumerate(messages)
             if getattr(message, "type", None) == "human"),
            default=-1,
        ) + 1
        artifacts: list[dict[str, Any]] = []
        final_text = assistant_text
        revision_rejection = ""
        agent_turns = 0
        tool_calls = 0
        cache_read = 0
        cache_read_seen = False
        cache_miss = 0
        cache_miss_seen = False
        planning_handoff = False
        handoff_itinerary_id = ""
        for message in messages[current_start:]:
            message_type = getattr(message, "type", None)
            if message_type == "tool":
                tool_calls += 1
                try:
                    tool_payload = json.loads(str(getattr(message, "content", "") or "{}"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    tool_payload = {}
                if getattr(message, "name", None) == "start_revision":
                    revision_rejection = (
                        str(tool_payload.get("message") or "")
                        if tool_payload.get("ok") is False else ""
                    )
                if (
                    tool_payload.get("ok") is True
                    and isinstance(tool_payload.get("data"), dict)
                    and tool_payload["data"].get("itinerary_id")
                ):
                    planning_handoff = True
                    handoff_itinerary_id = str(tool_payload["data"]["itinerary_id"])
                artifact = getattr(message, "artifact", None)
                if isinstance(artifact, dict):
                    artifacts.append(artifact)
            elif message_type == "ai":
                agent_turns += 1
                content = self._message_text(message)
                if content and not getattr(message, "tool_calls", None):
                    final_text = content
                usage = getattr(message, "usage_metadata", None)
                details = usage.get("input_token_details") if isinstance(usage, dict) else None
                details = details if isinstance(details, dict) else {}
                cache_read_value = next((details[key] for key in ("cache_read", "cache_read_tokens")
                                         if key in details and isinstance(details[key], int)
                                         and not isinstance(details[key], bool) and details[key] >= 0), None)
                cache_miss_value = next((details[key] for key in ("cache_miss", "cache_creation")
                                         if key in details and isinstance(details[key], int)
                                         and not isinstance(details[key], bool) and details[key] >= 0), None)
                if cache_read_value is not None:
                    cache_read += cache_read_value
                    cache_read_seen = True
                if cache_miss_value is not None:
                    cache_miss += cache_miss_value
                    cache_miss_seen = True
        unique_artifacts: list[dict[str, Any]] = []
        artifact_keys: set[str] = set()
        for artifact in artifacts:
            key = str(artifact)
            if key not in artifact_keys:
                artifact_keys.add(key)
                unique_artifacts.append(artifact)
        safe_artifacts = validate_message_artifacts(unique_artifacts[:5])
        if not str(final_text or "").strip():
            final_text = "我已经处理了这条旅行消息。"
        if revision_rejection:
            # Rejections are already user-safe business messages. Do not let
            # an unconstrained model answer invent a different failure cause.
            final_text = revision_rejection
        metrics.increment("main_agent_turns", agent_turns)
        metrics.increment("main_agent_tool_calls", tool_calls)
        if cache_read_seen:
            metrics.increment("provider_cache_hit_tokens", cache_read)
        if cache_miss_seen:
            metrics.increment("provider_cache_miss_tokens", cache_miss)
        completion_fields = {"run_id": run["id"], "agent_turns": agent_turns, "tool_calls": tool_calls}
        if cache_read_seen:
            completion_fields["cache_hit_tokens"] = cache_read
        if cache_miss_seen:
            completion_fields["cache_miss_tokens"] = cache_miss
        metrics.log("main_agent_completed", **completion_fields)
        if planning_handoff:
            messages = await asyncio.to_thread(
                self.conversations.messages,
                run["user_id"], run["conversation_id"], limit=100,
            )
            final_message = next((
                item for item in reversed(messages)
                if item.get("role") == "assistant"
                and item.get("related_run_id") == run["id"]
                and item.get("related_itinerary_id") == handoff_itinerary_id
            ), None)
            if final_message:
                await self.manager.publish(
                    run["id"], "custom", {
                        "kind": "chat.message.completed",
                        "message_id": final_message["id"],
                        "content": final_message["content"],
                        "sequence": final_message["sequence"],
                        "created_at": final_message["created_at"],
                        "artifacts": final_message["artifacts"],
                        "related_itinerary_id": handoff_itinerary_id,
                    }, durable=True, validate_custom=False,
                )
            return {
                "planning_handoff": True,
                "result_itinerary_id": handoff_itinerary_id,
            }
        return await self.publish_assistant_message(
            run, str(final_text).strip(), artifacts=safe_artifacts
        )

    @staticmethod
    def _message_text(message: Any) -> str:
        content = getattr(message, "content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                str(item.get("text") or "") for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            )
        return ""

    def _conversation_targets(
        self, user_id: str, conversation_id: str
    ) -> list[dict[str, Any]]:
        runs = self.runs.list(user_id, conversation_id=conversation_id, limit=20)
        targets: list[dict[str, Any]] = []
        for item in runs:
            if item["kind"] not in {RunKind.TRAVEL_PLAN.value, RunKind.REVISION.value}:
                continue
            snapshot = item["request_snapshot"]
            itinerary_id = item.get("result_itinerary_id") or snapshot.get(
                "related_itinerary_id"
            )
            targets.append(
                {
                    "run_id": item["id"],
                    "itinerary_id": itinerary_id,
                    "kind": item["kind"],
                    "status": item["status"],
                    "destination": snapshot.get("destination", ""),
                }
            )
        return targets

    def _owned_itinerary_summary(
        self, user_id: str, itinerary_id: str | None
    ) -> dict[str, Any]:
        if not itinerary_id:
            raise OwnedResourceNotFound("base itinerary not found")
        with itinerary_connection(self.manager.db_path) as conn:
            row = get_itinerary_record(itinerary_id, user_id, conn)
        if not row:
            raise OwnedResourceNotFound("base itinerary not found")
        return {
            field: row[field]
            for field in ("id", "destination", "start_date", "end_date", "version", "query")
        }

    def _itinerary_is_modifiable(self, user_id: str, itinerary_id: str) -> bool:
        """Check the private checkpoint without ever placing it in LLM context."""
        with itinerary_connection(self.manager.db_path) as conn:
            row = get_itinerary_record(itinerary_id, user_id, conn)
        return bool(row and row["planner_state_json"])

    @staticmethod
    def _brief_context(brief: dict[str, Any] | None) -> dict[str, Any] | None:
        if not brief:
            return None
        return {
            "id": brief["id"],
            "status": brief["status"],
            "data": brief["data"],
            "missing_fields": brief["missing_fields"],
            "memory_context": brief.get("memory_context"),
            "effective_constraints": brief.get("effective_constraints") or [],
            "constraint_coverage": brief.get("constraint_coverage") or [],
        }

    @staticmethod
    def _snapshot_query(snapshot: dict[str, Any]) -> str:
        parts = [str(snapshot.get("destination") or "").strip()]
        if snapshot.get("start_date") and snapshot.get("end_date"):
            parts.append(f"{snapshot['start_date']}至{snapshot['end_date']}")
        elif snapshot.get("days"):
            parts.append(f"{snapshot['days']}日游")
        if snapshot.get("trip_budget"):
            parts.append(f"本次预算：{snapshot['trip_budget']}")
        for item in snapshot.get("effective_constraints") or []:
            if item.get("value_text"):
                parts.append(str(item["value_text"]))
        return "，".join(part for part in parts if part)

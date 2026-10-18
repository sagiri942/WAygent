"""Adapter from immutable travel-plan Run snapshots to TravelPlanState."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from app.core.itinerary_storage import itinerary_connection
from app.core.memory import (
    load_itinerary,
    save_itinerary,
)
from app.core.travel_memory import MemoryRepository
from app.core.planning_constraints import constraints_for_prompt
from app.planning.schemas import TravelPlanState
from app.planning.shadow_profiles import launch_shadow_profiles
from app.planning.restaurant_enrichment import route_fingerprint
from app.planning.tip_enrichment import (
    generate_tip_enrichment, load_tip_enrichment, save_tip_enrichment,
)
from app.runtime.models import PublicError, RunKind, RunStatus
from app.runtime.manager import RunManager
from app.runtime.repositories import ConversationRepository


def snapshot_to_state(snapshot: dict[str, Any]) -> TravelPlanState:
    allowed = set(TravelPlanState.model_fields)
    values = {key: value for key, value in snapshot.items() if key in allowed}
    values.setdefault("travel_start_date", snapshot.get("start_date"))
    values.setdefault("travel_end_date", snapshot.get("end_date"))
    if not values.get("query"):
        destination = values.get("destination") or snapshot.get("destination") or ""
        days = values.get("days") or snapshot.get("days") or ""
        values["query"] = f"{destination}{days}日游".strip()
    return TravelPlanState(**values)


async def planning_run_to_state(
    run: dict[str, Any], *, memory_repository=None
) -> TravelPlanState:
    facts = run["request_snapshot"].get("memory_profile_snapshot")
    if facts is None:
        if memory_repository is None:
            raise RuntimeError(
                "Planning Run has no frozen memory snapshot and memory repository is not configured"
            )
        _revision, facts = await asyncio.to_thread(
            memory_repository.snapshot, run["user_id"]
        )
    effective = run["request_snapshot"].get("effective_constraints") or []
    snapshot = {
        **run["request_snapshot"],
        "profile_hint": (
            constraints_for_prompt(effective)
            if effective else MemoryRepository.format_for_prompt(facts)
        ) or None,
    }
    return snapshot_to_state(snapshot)


class PlanningFinalizer:
    def __init__(
        self, manager: RunManager, on_tip_queued: Callable[[], None] | None = None,
        *, tips_as_run: bool = True,
        expected_parent_lock_version: int | None = None,
        require_latest_parent: bool = False,
        db_path=None,
    ):
        self.manager = manager
        self.on_tip_queued = on_tip_queued
        self.tips_as_run = tips_as_run
        self.expected_parent_lock_version = expected_parent_lock_version
        self.require_latest_parent = require_latest_parent
        self.db_path = manager.db_path if db_path is None else db_path

    async def __call__(
        self,
        run: dict[str, Any],
        updates: dict[str, Any],
        _assistant_text: str,
    ) -> dict[str, Any] | None:
        state = TravelPlanState(
            **{
                **snapshot_to_state(run["request_snapshot"]).model_dump(),
                **updates,
            }
        )
        if state.revision_scope:
            from app.planning.nodes import PlanningQualityError, WalkingTimePlanningError
            if (
                not self.require_latest_parent
                or self.expected_parent_lock_version is None
                or not state.parent_plan_id
            ):
                raise PlanningQualityError("full revision finalizer requires an optimistic parent-version guard")
            if state.approved is not True or not (state.quality_report or {}).get("passed") is True:
                raise PlanningQualityError("full revision finalizer rejected a route without an approved quality report")
            verification = state.walking_time_verification or {}
            if verification.get("status") != "verified" and verification.get("verified") is not True:
                raise WalkingTimePlanningError({
                    **verification, "status": verification.get("status") or "unverified",
                    "verified": False,
                })
        if not state.final_plan:
            raise RuntimeError("planning graph completed without an itinerary")
        persist = lambda: self._persist(
            run, state,
            expected_parent_lock_version=self.expected_parent_lock_version,
            require_latest_parent=self.require_latest_parent,
            db_path=self.db_path,
        )
        if state.revision_scope:
            itinerary_id = await self.manager.persist_revision(run["id"], persist)
        else:
            itinerary_id = await asyncio.to_thread(persist)
        # Shadow profiles reuse the frozen pool but never delay the formal run.
        launch_shadow_profiles(itinerary_id, run["user_id"], state)
        await self.manager.publish(
            run["id"],
            "custom",
            {
                "kind": "planning.itinerary_created",
                "itinerary_id": itinerary_id,
                "destination": str(state.destination or ""),
                **({"revision_scope": state.revision_scope} if state.revision_scope else {}),
            },
            durable=True,
        )
        if run.get("conversation_id"):
            destination = str(state.destination or "这趟旅行")
            message = await asyncio.to_thread(
                ConversationRepository(self.manager.db_path).add_message,
                run["user_id"], run["conversation_id"], "assistant",
                f"{destination}的完整行程已经准备好了。路线、开放时间、餐饮和游玩提示都已整理，可以打开查看完整方案。",
                related_run_id=run["id"], related_itinerary_id=itinerary_id,
            )
            await self.manager.publish(
                run["id"], "custom", {
                    "kind": "chat.message.completed",
                    "message_id": message["id"],
                    "content": message["content"],
                    "sequence": message["sequence"],
                    "created_at": message["created_at"],
                    "artifacts": message["artifacts"],
                    "related_itinerary_id": itinerary_id,
                }, durable=True, validate_custom=False,
            )
        if self.tips_as_run:
            self.manager.create(
                user_id=run["user_id"], kind=RunKind.SPOT_TIPS,
                conversation_id=run.get("conversation_id"), itinerary_id=itinerary_id,
                request_snapshot={"itinerary_id": itinerary_id, "route_fingerprint": route_fingerprint(state.final_plan)},
            )
            if self.on_tip_queued:
                self.on_tip_queued()
            await self.manager.publish_itinerary(
                itinerary_id,
                {"kind": "itinerary.tip_status_changed", "itinerary_id": itinerary_id, "status": "queued"},
            )
        else:
            _launch_silent_tip_task(
                self.manager, itinerary_id, run["user_id"],
                state.final_plan, state.model_dump(mode="json"),
            )
        return {"result_itinerary_id": itinerary_id}

    @staticmethod
    def _persist(
        run: dict[str, Any], state: TravelPlanState, *,
        expected_parent_lock_version: int | None = None,
        require_latest_parent: bool = False, db_path=None,
    ) -> str:
        checkpoint = {
            key: value
            for key, value in state.model_dump(mode="json").items()
            if key != "final_plan"
        }
        with itinerary_connection(db_path) as conn:
            itinerary_id = save_itinerary(
                run["user_id"],
                state.final_plan,
                state.query,
                conn,
                parent_id=state.parent_plan_id,
                modification_notes=state.modification_notes,
                planner_state=checkpoint,
                expected_parent_lock_version=expected_parent_lock_version,
                require_latest_parent=require_latest_parent,
            )
        return itinerary_id


def _launch_silent_tip_task(
    manager: RunManager,
    itinerary_id: str,
    user_id: str,
    plan: dict[str, Any],
    planner_state: dict[str, Any],
) -> None:
    """Enrich tips without creating another public or persisted Run."""
    fingerprint = route_fingerprint(plan)

    async def work() -> None:
        await manager.publish_itinerary(
            itinerary_id,
            {"kind": "itinerary.tip_status_changed", "itinerary_id": itinerary_id, "status": "running"},
        )
        status = "failed"
        try:
            tips = await generate_tip_enrichment(plan, planner_state)
            with itinerary_connection(manager.db_path) as conn:
                latest = load_itinerary(itinerary_id, conn)
                if latest and route_fingerprint(latest["plan"]) == fingerprint:
                    existing = load_tip_enrichment(itinerary_id, user_id, fingerprint, conn)
                    merged_tips = {**((existing or {}).get("tips") or {}), **tips}
                    save_tip_enrichment(itinerary_id, user_id, fingerprint, merged_tips, conn)
                    status = "succeeded"
                else:
                    status = "cancelled"
        except Exception:
            status = "failed"
        await manager.publish_itinerary(
            itinerary_id,
            {"kind": "itinerary.tip_status_changed", "itinerary_id": itinerary_id, "status": status},
        )

    asyncio.create_task(work(), name=f"spot-tips:{itinerary_id}")


class SpotTipsWorker:
    def __init__(self, manager: RunManager):
        self.manager = manager

    async def __call__(self, run: dict[str, Any], _cancel_event: asyncio.Event) -> None:
        itinerary_id = str(run["request_snapshot"].get("itinerary_id") or "")
        with itinerary_connection(self.manager.db_path) as conn:
            data = load_itinerary(itinerary_id, conn)
        if not data:
            raise ValueError("itinerary not found")
        plan = data["plan"]
        fingerprint = str(run["request_snapshot"].get("route_fingerprint") or "")
        if fingerprint != route_fingerprint(plan):
            await self.manager.transition(run["id"], RunStatus.CANCELLED)
            await self.manager.publish_itinerary(itinerary_id, {"kind": "itinerary.tip_status_changed", "itinerary_id": itinerary_id, "status": "cancelled"})
            return None
        await self.manager.publish_itinerary(itinerary_id, {"kind": "itinerary.tip_status_changed", "itinerary_id": itinerary_id, "status": "running"})
        try:
            tips = await generate_tip_enrichment(plan, data.get("planner_state") or {})
            written = False
            with itinerary_connection(self.manager.db_path) as conn:
                latest = load_itinerary(itinerary_id, conn)
                if latest and route_fingerprint(latest["plan"]) == fingerprint:
                    existing = load_tip_enrichment(itinerary_id, run["user_id"], fingerprint, conn)
                    merged_tips = {**((existing or {}).get("tips") or {}), **tips}
                    save_tip_enrichment(itinerary_id, run["user_id"], fingerprint, merged_tips, conn)
                    written = True
            if written:
                status = "succeeded"
            else:
                await self.manager.transition(run["id"], RunStatus.CANCELLED)
                status = "cancelled"
        except Exception as exc:  # The enrichment must not poison the core itinerary run.
            await self.manager.transition(
                run["id"], RunStatus.FAILED,
                error_public=PublicError(
                    code="spot_tips_failed",
                    message="景点贴士暂未生成，可稍后手动重试",
                    retryable=True,
                ).model_dump(),
                error_internal=f"{type(exc).__name__}: {exc}",
            )
            status = "failed"
        await self.manager.publish_itinerary(itinerary_id, {"kind": "itinerary.tip_status_changed", "itinerary_id": itinerary_id, "status": status})
        return None

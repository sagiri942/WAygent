"""Non-blocking shadow-profile solves over the frozen formal candidate pool."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from app.core.itinerary_storage import itinerary_connection
from app.planning.helpers import haversine_km
from app.planning.optimizer import AttractionSubsetOptimizer, daily_bounds_from_constraints
from app.planning.restaurant_enrichment import route_fingerprint
from app.planning.schemas import TravelPlanState

_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()


def configured_shadow_profiles() -> list[str]:
    active = os.getenv("PLANNING_SHADOW_PROFILES", "")
    return list(dict.fromkeys(item.strip() for item in active.split(",") if item.strip()))


def _selected(route: list[dict[str, Any]]) -> set[str]:
    return {spot["name"] for day in route for spot in day.get("spots") or []}


def _coverage(route: list[dict[str, Any]]) -> set[tuple[int, str, str]]:
    return {
        (int(day["day"]), spot["name"], spot["meal_coverage"])
        for day in route
        for spot in day.get("spots") or []
        if spot.get("meal_coverage")
    }


def _distance(route: list[dict[str, Any]], candidates: dict[str, dict[str, Any]]) -> float:
    total = 0.0
    for day in route:
        names = [spot["name"] for spot in day.get("spots") or []]
        total += sum(
            haversine_km(candidates[left]["location"], candidates[right]["location"])
            for left, right in zip(names, names[1:])
        )
    return round(total, 6)


def compare_shadow_routes(
    active_route: list[dict[str, Any]],
    shadow_route: list[dict[str, Any]],
    candidates: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    active_selected = _selected(active_route)
    shadow_selected = _selected(shadow_route)
    union = active_selected | shadow_selected
    return {
        "selected_intersection": sorted(active_selected & shadow_selected),
        "active_only": sorted(active_selected - shadow_selected),
        "shadow_only": sorted(shadow_selected - active_selected),
        "selected_jaccard": 1.0 if not union else round(len(active_selected & shadow_selected) / len(union), 6),
        "active_meal_coverage": sorted(_coverage(active_route)),
        "shadow_meal_coverage": sorted(_coverage(shadow_route)),
        "active_distance_km": _distance(active_route, candidates),
        "shadow_distance_km": _distance(shadow_route, candidates),
        "user_edit_observation": None,
    }


def launch_shadow_profiles(itinerary_id: str, user_id: str, state: TravelPlanState) -> None:
    profiles = [
        profile for profile in configured_shadow_profiles()
        if profile != state.scoring_profile_version
    ]
    if not profiles or not state.candidate_pool_frozen or not state.candidate_pool_fingerprint:
        return
    snapshot = state.model_copy(deep=True)
    for profile in profiles:
        task = asyncio.create_task(_run_shadow(itinerary_id, user_id, profile, snapshot))
        _BACKGROUND_TASKS.add(task)
        task.add_done_callback(_BACKGROUND_TASKS.discard)


async def _run_shadow(
    itinerary_id: str,
    user_id: str,
    profile_version: str,
    state: TravelPlanState,
) -> None:
    shadow_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with itinerary_connection() as conn:
        if hasattr(conn, "dialect"):
            from sqlalchemy.dialects.mysql import insert
            from app.core.identity_schema import optimizer_shadow_runs
            statement = insert(optimizer_shadow_runs).values(
                id=shadow_id, itinerary_id=itinerary_id, user_id=user_id,
                profile_version=profile_version,
                candidate_pool_fingerprint=state.candidate_pool_fingerprint,
                status="running", created_at=now, updated_at=now,
            )
            # MySQL requires an update clause for duplicate-key handling; assign
            # the stable primary key to itself, matching SQLite INSERT OR IGNORE.
            conn.execute(statement.on_duplicate_key_update(id=optimizer_shadow_runs.c.id))
        else:
            conn.execute(
                """INSERT OR IGNORE INTO optimizer_shadow_runs(
                   id,itinerary_id,user_id,profile_version,candidate_pool_fingerprint,
                   status,created_at,updated_at) VALUES(?,?,?,?,?,'running',?,?)""",
                (shadow_id, itinerary_id, user_id, profile_version, state.candidate_pool_fingerprint, now, now),
            )
    try:
        user_min, user_max = daily_bounds_from_constraints(state.effective_constraints)
        result = await asyncio.to_thread(
            AttractionSubsetOptimizer(profile_version).solve,
            state.candidate_pool,
            days=state.days,
            habit_preference=state.habit_preference,
            max_per_day=state.max_per_day,
            weather_by_day={index + 1: item for index, item in enumerate(state.weather_forecast)},
            travel_start_date=state.travel_start_date,
            user_daily_min=user_min,
            user_daily_max=user_max,
        )
        candidates = {item["poi_name"]: item for item in state.candidate_pool}
        payload = {
            "profile_version": profile_version,
            "diagnostics": result.diagnostics.model_dump(),
            "comparison": compare_shadow_routes(state.route, result.route, candidates),
        }
        with itinerary_connection() as conn:
            if hasattr(conn, "dialect"):
                from sqlalchemy import update
                from app.core.identity_schema import optimizer_shadow_runs
                conn.execute(update(optimizer_shadow_runs).where(
                    optimizer_shadow_runs.c.itinerary_id == itinerary_id,
                    optimizer_shadow_runs.c.profile_version == profile_version,
                    optimizer_shadow_runs.c.candidate_pool_fingerprint == state.candidate_pool_fingerprint,
                ).values(status="succeeded", result_json=json.dumps(payload, ensure_ascii=False),
                         updated_at=datetime.now(timezone.utc).isoformat()))
            else:
                conn.execute(
                    "UPDATE optimizer_shadow_runs SET status='succeeded',result_json=?,updated_at=? "
                    "WHERE itinerary_id=? AND profile_version=? AND candidate_pool_fingerprint=?",
                    (json.dumps(payload, ensure_ascii=False), datetime.now(timezone.utc).isoformat(), itinerary_id, profile_version, state.candidate_pool_fingerprint),
                )
    except Exception as exc:
        with itinerary_connection() as conn:
            if hasattr(conn, "dialect"):
                from sqlalchemy import update
                from app.core.identity_schema import optimizer_shadow_runs
                conn.execute(update(optimizer_shadow_runs).where(
                    optimizer_shadow_runs.c.itinerary_id == itinerary_id,
                    optimizer_shadow_runs.c.profile_version == profile_version,
                    optimizer_shadow_runs.c.candidate_pool_fingerprint == state.candidate_pool_fingerprint,
                ).values(status="failed", error_text=f"{type(exc).__name__}: {exc}",
                         updated_at=datetime.now(timezone.utc).isoformat()))
            else:
                conn.execute(
                    "UPDATE optimizer_shadow_runs SET status='failed',error_text=?,updated_at=? "
                    "WHERE itinerary_id=? AND profile_version=? AND candidate_pool_fingerprint=?",
                    (f"{type(exc).__name__}: {exc}", datetime.now(timezone.utc).isoformat(), itinerary_id, profile_version, state.candidate_pool_fingerprint),
                )


def record_route_edit(
    itinerary_id: str,
    plan: dict[str, Any],
    conn: sqlite3.Connection,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    if hasattr(conn, "dialect"):
        from sqlalchemy import update, func
        from app.core.identity_schema import optimizer_shadow_runs
        conn.execute(update(optimizer_shadow_runs).where(
            optimizer_shadow_runs.c.itinerary_id == itinerary_id
        ).values(first_user_edit_at=func.coalesce(optimizer_shadow_runs.c.first_user_edit_at, now),
                 edited_route_fingerprint=route_fingerprint(plan), updated_at=now))
    else:
        conn.execute(
            "UPDATE optimizer_shadow_runs SET first_user_edit_at=COALESCE(first_user_edit_at,?),"
            "edited_route_fingerprint=?,updated_at=? WHERE itinerary_id=?",
            (now, route_fingerprint(plan), now, itinerary_id),
        )

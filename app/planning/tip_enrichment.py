"""Asynchronous, route-versioned attraction-tip enrichment."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import date, datetime, timezone
from typing import Any

from app.planning.nodes import make_spot_tips_node
from app.planning.restaurant_enrichment import route_fingerprint
from app.planning.schemas import TravelPlanState
from app.planning.revision_scope import local_revision_day


def load_tip_enrichment(itinerary_id: str, user_id: str, fingerprint: str, conn: sqlite3.Connection) -> dict[str, Any] | None:
    if not isinstance(conn, sqlite3.Connection):
        from sqlalchemy import select
        from app.core.identity_schema import itinerary_enrichments
        row = conn.execute(select(itinerary_enrichments.c.payload_json).where(
            itinerary_enrichments.c.itinerary_id == itinerary_id,
            itinerary_enrichments.c.user_id == user_id,
            itinerary_enrichments.c.kind == "spot_tips",
            itinerary_enrichments.c.route_fingerprint == fingerprint,
        )).first()
        return json.loads(row[0]) if row else None
    row = conn.execute(
        "SELECT payload_json FROM itinerary_enrichments WHERE itinerary_id=? AND user_id=? AND kind='spot_tips' AND route_fingerprint=?",
        (itinerary_id, user_id, fingerprint),
    ).fetchone()
    return json.loads(row["payload_json"]) if row else None


def save_tip_enrichment(itinerary_id: str, user_id: str, fingerprint: str, tips: dict[str, str], conn: sqlite3.Connection) -> None:
    now = datetime.now(timezone.utc).isoformat()
    if not isinstance(conn, sqlite3.Connection):
        from sqlalchemy.dialects.mysql import insert
        from app.core.identity_schema import itinerary_enrichments
        statement = insert(itinerary_enrichments).values(
            id=str(uuid.uuid4()), itinerary_id=itinerary_id, user_id=user_id,
            kind="spot_tips", route_fingerprint=fingerprint,
            payload_json=json.dumps({"route_fingerprint": fingerprint, "tips": tips}, ensure_ascii=False),
            created_at=now, updated_at=now,
        )
        conn.execute(statement.on_duplicate_key_update(
            payload_json=statement.inserted.payload_json,
            updated_at=statement.inserted.updated_at,
        ))
        return
    conn.execute(
        """INSERT INTO itinerary_enrichments(id,itinerary_id,user_id,kind,route_fingerprint,payload_json,created_at,updated_at)
           VALUES(?,?,?,'spot_tips',?,?,?,?)
           ON CONFLICT(itinerary_id,kind,route_fingerprint) DO UPDATE SET payload_json=excluded.payload_json,updated_at=excluded.updated_at""",
        (str(uuid.uuid4()), itinerary_id, user_id, fingerprint,
         json.dumps({"route_fingerprint": fingerprint, "tips": tips}, ensure_ascii=False), now, now),
    )


async def generate_tip_enrichment(plan: dict[str, Any], planner_state: dict[str, Any]) -> dict[str, str]:
    route = []
    target_day = local_revision_day(planner_state)
    for day in plan.get("days") or []:
        if target_day is not None and int(day.get("day") or 0) != target_day:
            continue
        route.append({
            "day": day.get("day"), "theme": day.get("theme", ""),
            "spots": [
                {"name": item.get("name"), "period": item.get("period"),
                 "start_time": item.get("start_time"), "end_time": item.get("end_time")}
                for item in day.get("timeline") or [] if item.get("type") == "attraction"
            ],
        })
    state = TravelPlanState(
        query=str(planner_state.get("query") or plan.get("query") or "旅行行程"),
        destination=plan.get("destination"),
        days=int(planner_state.get("days") or len(plan.get("days") or route)), route=route,
        travel_start_date=(date.fromisoformat(plan["start_date"]) if plan.get("start_date") else None),
        weather_forecast=(
            planner_state.get("weather_forecast")
            or plan.get("weather_forecast") or []
        ),
        effective_constraints=(
            planner_state.get("effective_constraints")
            or plan.get("effective_constraints") or []
        ),
    )
    result = await make_spot_tips_node(None)(state)
    tips = dict(result.get("spot_tips") or {})
    if any(day["spots"] for day in route) and not tips:
        # The legacy graph node intentionally swallows provider failures so a
        # formal planning run can finish.  In the independent worker an empty
        # result is a failed enrichment: persisting it as success would hide
        # the retry action from the user.
        raise RuntimeError("spot tips returned no usable attraction tips")
    return tips

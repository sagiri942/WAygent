"""On-demand restaurant enrichment over an immutable attraction route."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from app.planning.helpers import restaurant_to_dict


AroundSearch = Callable[..., list[dict[str, Any]]]


def route_fingerprint(plan: dict[str, Any]) -> str:
    """Hash only route anchors and meal coverage, never enrichment metadata."""

    core = []
    for day in plan.get("days") or []:
        core.append({
            "day": day.get("day"),
            "attractions": [
                {
                    "name": item.get("name"),
                    "start_time": item.get("start_time"),
                    "end_time": item.get("end_time"),
                    "location": item.get("location"),
                    "meal_coverage": item.get("meal_coverage"),
                }
                for item in day.get("timeline") or []
                if item.get("type") == "attraction"
            ],
        })
    payload = json.dumps(core, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _time_minute(value: Any) -> int | None:
    text = str(value or "")
    try:
        hour, minute = text.split(":", 1)
        return int(hour) * 60 + int(minute)
    except (ValueError, AttributeError):
        return None


def _anchor(attractions: list[dict[str, Any]], meal: str) -> dict[str, Any] | None:
    target = 12 * 60 + 30 if meal == "lunch" else 18 * 60 + 30
    located = [item for item in attractions if item.get("location")]
    if not located:
        return None
    return min(
        located,
        key=lambda item: (
            abs(((_time_minute(item.get("start_time")) or target) + (_time_minute(item.get("end_time")) or target)) / 2 - target),
            str(item.get("name") or ""),
        ),
    )


def _pick_restaurant(raw_items: list[dict[str, Any]], used: set[str]) -> dict[str, Any] | None:
    candidates = [restaurant_to_dict(item) for item in raw_items]
    candidates = [item for item in candidates if item and item.get("name") not in used]
    if not candidates:
        return None
    candidates.sort(
        key=lambda item: (
            -float(item.get("rating") or 0),
            float(item.get("distance") or 999999),
            str(item.get("name") or ""),
        )
    )
    picked = candidates[0]
    used.add(str(picked["name"]))
    return picked


def generate_restaurant_enrichment(
    plan: dict[str, Any],
    *,
    api_key: str,
    search_around: AroundSearch,
) -> dict[str, Any]:
    """Recommend only meals not already covered by an attraction."""

    fingerprint = route_fingerprint(plan)
    used: set[str] = set()
    days_out: list[dict[str, Any]] = []
    for day in plan.get("days") or []:
        attractions = [
            item for item in day.get("timeline") or [] if item.get("type") == "attraction"
        ]
        meal_results = []
        for meal in ("lunch", "dinner"):
            covered = next(
                (item for item in attractions if item.get("meal_coverage") == meal),
                None,
            )
            if covered:
                meal_results.append({
                    "meal": meal,
                    "covered_by_attraction": covered.get("name"),
                    "external_restaurant": None,
                    "food_street_suggestions": [],
                })
                continue
            anchor = _anchor(attractions, meal)
            restaurant = None
            error = None
            if anchor:
                try:
                    raw = search_around(
                        anchor["location"], api_key,
                        types="餐饮服务", radius=1500, offset=10,
                    )
                    restaurant = _pick_restaurant(raw, used)
                except RuntimeError as exc:
                    error = str(exc)
            meal_results.append({
                "meal": meal,
                "covered_by_attraction": None,
                "external_restaurant": restaurant,
                "food_street_suggestions": [],
                "anchor_attraction": anchor.get("name") if anchor else None,
                **({"error": error} if error else {}),
            })
        days_out.append({"day": day.get("day"), "meals": meal_results})
    return {
        "kind": "restaurant",
        "route_fingerprint": fingerprint,
        "days": days_out,
    }


def load_restaurant_enrichment(
    itinerary_id: str,
    user_id: str,
    fingerprint: str,
    conn: sqlite3.Connection,
) -> dict[str, Any] | None:
    if not isinstance(conn, sqlite3.Connection):
        from sqlalchemy import select
        from app.core.identity_schema import itinerary_enrichments
        row = conn.execute(select(itinerary_enrichments.c.payload_json).where(
            itinerary_enrichments.c.itinerary_id == itinerary_id,
            itinerary_enrichments.c.user_id == user_id,
            itinerary_enrichments.c.kind == "restaurant",
            itinerary_enrichments.c.route_fingerprint == fingerprint,
        )).first()
        return json.loads(row[0]) if row else None
    row = conn.execute(
        "SELECT payload_json FROM itinerary_enrichments "
        "WHERE itinerary_id=? AND user_id=? AND kind='restaurant' AND route_fingerprint=?",
        (itinerary_id, user_id, fingerprint),
    ).fetchone()
    return json.loads(row["payload_json"]) if row else None


def save_restaurant_enrichment(
    itinerary_id: str,
    user_id: str,
    payload: dict[str, Any],
    conn: sqlite3.Connection,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    enrichment_id = str(uuid.uuid4())
    if not isinstance(conn, sqlite3.Connection):
        from sqlalchemy.dialects.mysql import insert
        from app.core.identity_schema import itinerary_enrichments
        statement = insert(itinerary_enrichments).values(
            id=enrichment_id, itinerary_id=itinerary_id, user_id=user_id,
            kind="restaurant", route_fingerprint=payload["route_fingerprint"],
            payload_json=json.dumps(payload, ensure_ascii=False), created_at=now, updated_at=now,
        )
        conn.execute(statement.on_duplicate_key_update(
            payload_json=statement.inserted.payload_json,
            updated_at=statement.inserted.updated_at,
        ))
        return payload
    conn.execute(
        """INSERT INTO itinerary_enrichments(
           id,itinerary_id,user_id,kind,route_fingerprint,payload_json,created_at,updated_at)
           VALUES(?,?,?,'restaurant',?,?,?,?)
           ON CONFLICT(itinerary_id,kind,route_fingerprint) DO UPDATE SET
             payload_json=excluded.payload_json,updated_at=excluded.updated_at""",
        (
            enrichment_id, itinerary_id, user_id, payload["route_fingerprint"],
            json.dumps(payload, ensure_ascii=False), now, now,
        ),
    )
    return payload

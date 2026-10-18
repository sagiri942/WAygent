"""历史行程 API。"""

from __future__ import annotations

from app.planning.revision_scope import local_revision_day

import asyncio
import copy
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.core.auth import decode_token
from app.core.itinerary_storage import itinerary_connection
from app.core.memory import itinerary_owner, list_itineraries, list_itineraries_page, load_itinerary
from app.planning.restaurant_enrichment import route_fingerprint
from app.planning.tip_enrichment import load_tip_enrichment
from app.runtime.container import manager, scheduler
from app.runtime.models import RunKind

router = APIRouter(prefix="/api/history", tags=["history"])


def configure_history_runtime(*, manager_backend, scheduler_backend) -> None:
    """Use the same application Runtime for tip retries and live updates."""
    global manager, scheduler
    manager = manager_backend
    scheduler = scheduler_backend


def _require_user(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "未登录")
    user_id = decode_token(authorization[7:])
    if not user_id:
        raise HTTPException(401, "token 无效或已过期")
    return user_id


def _owned_itinerary(plan_id: str, user_id: str, conn):
    owner = itinerary_owner(plan_id, conn)
    if not owner:
        raise HTTPException(404, "行程不存在")
    if owner != user_id:
        raise HTTPException(403, "无权访问")
    data = load_itinerary(plan_id, conn)
    if not data:
        raise HTTPException(404, "行程不存在")
    return data


def _tip_run_status(plan_id: str, fingerprint: str, conn) -> str | None:
    """Find the newest independent tip run for this immutable route version."""
    if hasattr(conn, "dialect"):
        from sqlalchemy import select
        from app.core.identity_schema import runs
        rows = conn.execute(select(runs.c.status, runs.c.request_snapshot_json).where(
            runs.c.kind == "spot_tips"
        ).order_by(runs.c.created_at.desc())).mappings().all()
    else:
        rows = conn.execute(
            "SELECT status,request_snapshot_json FROM runs WHERE kind='spot_tips' ORDER BY created_at DESC"
        ).fetchall()
    for row in rows:
        try:
            snapshot = json.loads(row["request_snapshot_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        if snapshot.get("itinerary_id") == plan_id and snapshot.get("route_fingerprint") == fingerprint:
            return str(row["status"])
    return None


def _project_tip_enrichment(data: dict, user_id: str, conn) -> dict:
    """Overlay route-versioned tips without modifying persisted core route JSON."""
    projected = copy.deepcopy(data)
    plan = projected["plan"]
    fingerprint = route_fingerprint(plan)
    enrichment = load_tip_enrichment(projected["id"], user_id, fingerprint, conn)
    tips = (enrichment or {}).get("tips") or {}
    local_day = local_revision_day(projected.get("planner_state") or {})
    required_tips = [
        str(item.get("name") or "")
        for day in plan.get("days") or []
        if local_day is None or int(day.get("day") or 0) == local_day
        for item in day.get("timeline") or []
        if item.get("type") == "attraction" and item.get("name")
    ]
    local_cache_complete = bool(required_tips) and all(
        name in tips and tips[name] for name in required_tips
    )
    has_tips = local_cache_complete if local_day is not None else bool(tips)
    for day in plan.get("days") or []:
        for item in day.get("timeline") or []:
            if item.get("type") == "attraction":
                item["tip"] = tips.get(str(item.get("name") or "")) or None
    run_status = _tip_run_status(projected["id"], fingerprint, conn)
    # A historical empty payload came from the former in-graph fallback.  It
    # is not a useful completed enrichment and must remain retryable.
    plan["tip_status"] = "succeeded" if has_tips else (
        "failed" if run_status == "succeeded" else (run_status or "unavailable")
    )
    plan["tip_route_fingerprint"] = fingerprint
    return projected


@router.get("")
def get_history(
    authorization: str | None = Header(default=None),
    limit: int | None = Query(default=None, ge=1, le=20),
    cursor: str | None = Query(default=None),
):
    user_id = _require_user(authorization)
    if limit is None:
        with itinerary_connection() as conn:
            items = list_itineraries(user_id, conn)
        return items
    cursor_parts = None
    if cursor:
        try:
            created_at, itinerary_id = cursor.rsplit("|", 1)
            if not created_at or not itinerary_id:
                raise ValueError
            cursor_parts = (created_at, itinerary_id)
        except ValueError as exc:
            raise HTTPException(400, "历史记录游标无效") from exc
    with itinerary_connection() as conn:
        items, next_cursor = list_itineraries_page(
            user_id, conn, limit=limit, cursor=cursor_parts
        )
    return {
        "items": items,
        "next_cursor": "|".join(next_cursor) if next_cursor else None,
    }


@router.get("/{plan_id}")
def get_itinerary(plan_id: str, authorization: str | None = Header(default=None)):
    user_id = _require_user(authorization)
    with itinerary_connection() as conn:
        data = _owned_itinerary(plan_id, user_id, conn)
        return _project_tip_enrichment(data, user_id, conn)


@router.post("/{plan_id}/tips")
async def retry_itinerary_tips(plan_id: str, authorization: str | None = Header(default=None)):
    """Queue one user-requested retry for the route currently being displayed."""
    user_id = _require_user(authorization)
    with itinerary_connection() as conn:
        data = _owned_itinerary(plan_id, user_id, conn)
        fingerprint = route_fingerprint(data["plan"])
        existing = load_tip_enrichment(plan_id, user_id, fingerprint, conn)
        local_day = local_revision_day(data.get("planner_state") or {})
        tips = (existing or {}).get("tips") or {}
        required_tips = [
            str(item.get("name") or "")
            for day in data["plan"].get("days") or []
            if local_day is None or int(day.get("day") or 0) == local_day
            for item in day.get("timeline") or []
            if item.get("type") == "attraction" and item.get("name")
        ]
        local_cache_complete = bool(required_tips) and all(
            name in tips and tips[name] for name in required_tips
        )
        cache_complete = local_cache_complete if local_day is not None else bool(tips)
        if cache_complete:
            return {"status": "succeeded", "cached": True}
        active_status = _tip_run_status(plan_id, fingerprint, conn)
        if active_status in {"queued", "running"}:
            return {"status": active_status, "cached": True}
    run = manager.create(
        user_id=user_id,
        kind=RunKind.SPOT_TIPS,
        itinerary_id=plan_id,
        request_snapshot={"itinerary_id": plan_id, "route_fingerprint": fingerprint},
    )
    await manager.publish_itinerary(
        plan_id,
        {"kind": "itinerary.tip_status_changed", "itinerary_id": plan_id, "status": "queued"},
    )
    scheduler.notify()
    return {"status": "queued", "cached": False, "run": run}


@router.get("/{plan_id}/tips/stream")
async def stream_itinerary_tips(plan_id: str, authorization: str | None = Header(default=None)):
    user_id = _require_user(authorization)
    with itinerary_connection() as conn:
        _owned_itinerary(plan_id, user_id, conn)

    async def events() -> AsyncIterator[str]:
        async with manager.bridge.subscribe(f"itinerary:{plan_id}") as stream:
            async for item in stream:
                payload = json.dumps(item.payload, ensure_ascii=False)
                yield f"event: {item.kind}\ndata: {payload}\n\n"
                # Itinerary streams intentionally stay open after a terminal tip
                # job: the same detail page can receive a later manual retry.
                await asyncio.sleep(0)

    return StreamingResponse(
        events(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

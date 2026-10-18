"""Amap Web Service walking-route adapter used by planning verification."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from urllib.parse import urlencode

from app.core.http import http_get_json_async


AMAP_WALKING_URL = "https://restapi.amap.com/v3/direction/walking"

class WalkingRouteUnavailable(RuntimeError):
    """Amap explicitly did not return a usable walking route."""


class WalkingDurationMissing(ValueError):
    """The provider response omitted a valid walking duration."""


def _coordinate(point: Mapping[str, Any]) -> str:
    location = point.get("location")
    if not isinstance(location, Mapping):
        raise ValueError("walking route requires authoritative POI coordinates")
    try:
        longitude, latitude = float(location["lng"]), float(location["lat"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("walking route requires authoritative POI coordinates") from exc
    if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
        raise ValueError("walking route coordinates are out of range")
    return f"{longitude:.6f},{latitude:.6f}"


def walking_duration_minutes(response: Mapping[str, Any]) -> int:
    """Parse the v3 response's first route duration (seconds), rounded up."""
    if str(response.get("status")) != "1" or str(response.get("infocode")) != "10000":
        raise WalkingRouteUnavailable("Amap walking route was not verified")
    route = response.get("route")
    paths = route.get("paths") if isinstance(route, Mapping) else None
    if not isinstance(paths, list) or not paths or not isinstance(paths[0], Mapping):
        raise WalkingDurationMissing("Amap walking route response has no path")
    try:
        seconds = int(paths[0]["duration"])
    except (KeyError, TypeError, ValueError) as exc:
        raise WalkingDurationMissing("Amap walking route response has no valid duration") from exc
    if seconds <= 0:
        raise WalkingDurationMissing("Amap walking route duration must be positive")
    return (seconds + 59) // 60


async def query_walking_minutes(
    origin: Mapping[str, Any],
    destination: Mapping[str, Any],
    *,
    api_key: str | None = None,
    request: Callable[..., Awaitable[dict[str, Any]]] = http_get_json_async,
) -> int:
    """Query Amap's supported v3 walking direction endpoint for one leg."""
    key = (api_key if api_key is not None else os.getenv("AMAP_API_KEY", "")).strip()
    if not key:
        raise RuntimeError("AMAP_API_KEY is not configured")
    params = {
        "origin": _coordinate(origin),
        "destination": _coordinate(destination),
        "output": "JSON",
        "key": key,
    }
    url = f"{AMAP_WALKING_URL}?{urlencode(params)}"
    for attempt in range(3):
        payload = await request(url, timeout=8)
        # Amap reports QPS throttling in an HTTP-200 business response. The
        # shared HTTP transport retry cannot see it. Retry only this transient
        # code; cancellation and the enclosing walking/Run deadlines still win.
        if str(payload.get("status")) == "0" and str(payload.get("infocode")) == "10021" and attempt < 2:
            await asyncio.sleep(float(attempt + 1))
            continue
        break
    return walking_duration_minutes(payload)

"""Stable keys for reusing measured directed walking legs."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def walking_place_key(candidate: Any) -> str:
    """Identify an authoritative place across route and optimizer boundaries."""
    if isinstance(candidate, Mapping):
        name = str(candidate.get("poi_name") or candidate.get("name") or "").strip()
        poi_id = candidate.get("poi_id") or candidate.get("id")
        location = candidate.get("location") or {}
    else:
        name = str(getattr(candidate, "poi_name", "") or getattr(candidate, "name", "")).strip()
        poi_id = getattr(candidate, "poi_id", None)
        location = getattr(candidate, "location", None) or {}
    if poi_id:
        return f"id:{str(poi_id).strip()}"
    if isinstance(location, Mapping) and "lng" in location and "lat" in location:
        return f"place:{name.casefold()}@{float(location['lng']):.6f},{float(location['lat']):.6f}"
    return f"name:{name.casefold()}"


def walking_minutes_for(cache: Mapping[str, Mapping[str, int]] | None, origin: Any, destination: Any, default: int | None = None):
    """Read identity-keyed legs and retain compatibility with saved name-keyed legs."""
    if not cache:
        return default
    minutes = cache.get(walking_place_key(origin), {}).get(walking_place_key(destination))
    if minutes is None:
        origin_name = _place_name(origin)
        destination_name = _place_name(destination)
        minutes = cache.get(f"name:{origin_name.casefold()}", {}).get(f"name:{destination_name.casefold()}")
    if minutes is None:
        origin_name = _place_name(origin)
        destination_name = _place_name(destination)
        minutes = cache.get(origin_name, {}).get(destination_name)
    return default if minutes is None else minutes


def _place_name(candidate: Any) -> str:
    if isinstance(candidate, Mapping):
        return str(candidate.get("poi_name") or candidate.get("name") or "")
    return str(getattr(candidate, "poi_name", "") or getattr(candidate, "name", ""))


def walking_leg_minutes_for(cache: Mapping[tuple[str, str], int], origin: Any, destination: Any) -> int | None:
    origin_key, destination_key = walking_place_key(origin), walking_place_key(destination)
    minutes = cache.get((origin_key, destination_key))
    if minutes is None:
        origin_name = _place_name(origin)
        destination_name = _place_name(destination)
        minutes = cache.get((f"name:{origin_name.casefold()}", f"name:{destination_name.casefold()}"))
    if minutes is None:
        minutes = cache.get((_place_name(origin), _place_name(destination)))
    return minutes

"""Verify a newly optimized route with supplier walking durations."""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta

from app.planning.helpers import haversine_km
from app.planning.optimizer import (
    DAY_END,
    DAY_START,
    allowed_start_bounds,
    is_closed_on_date,
    meal_coverage_for_visit,
    format_minute,
)
from app.planning.walking_identity import walking_leg_minutes_for, walking_place_key

WalkingLegProvider = Callable[[Mapping, Mapping], Awaitable[int]]


@dataclass(frozen=True)
class WalkingTimeViolation:
    code: str
    day: int
    poi_name: str
    message: str


@dataclass(frozen=True)
class WalkingScheduleCheck:
    status: str
    route: list[dict]
    violations: tuple[WalkingTimeViolation, ...]
    correction_attempts: int = 0
    walking_time_minutes: dict[str, dict[str, int]] = field(default_factory=dict)
    new_queries: int = 0

    @property
    def passed(self) -> bool:
        return self.status == "verified" and not self.violations


class WalkingTimeUnavailable(RuntimeError):
    def __init__(self, code: str, day: int, origin: str, destination: str):
        self.code = code
        self.day = day
        self.origin = origin
        self.destination = destination
        super().__init__(f"无法核验第{day}天 {origin} 至 {destination} 的步行时间（{code}）")


def _scheduled_day(day_route, candidates, walking_minutes, visit_date):
    day = int(day_route.get("day", 0))
    violations = []
    spots = []
    cursor = DAY_START
    previous_candidate = None
    for source_spot in day_route.get("spots") or []:
        name = str(source_spot.get("name") or "")
        candidate = candidates.get(name)
        if candidate is None:
            violations.append(WalkingTimeViolation("UNKNOWN_POI", day, name, "地点不在本次权威候选中"))
            continue
        leg_minutes = None
        if previous_candidate is not None:
            leg_minutes = walking_leg_minutes_for(walking_minutes, previous_candidate, candidate)
            if leg_minutes is None:
                violations.append(WalkingTimeViolation("WALKING_TIME_MISSING", day, name, "相邻步行路段缺少供应商时长"))
                continue
            cursor += leg_minutes

        if is_closed_on_date(candidate.get("open_time"), visit_date):
            violations.append(WalkingTimeViolation("OPENING_TIME", day, name, "地点在出行日期闭馆"))
        bounds = allowed_start_bounds(candidate)
        if bounds is None:
            violations.append(WalkingTimeViolation("OPENING_TIME", day, name, "开放时间或预约时段不可满足"))
            start = cursor
        else:
            start = max(cursor, bounds[0])
            if start > bounds[1]:
                code = "FIXED_START" if candidate.get("fixed_start_min") is not None else "OPENING_TIME"
                violations.append(WalkingTimeViolation(code, day, name, "真实步行抵达时间晚于可访问时段"))
                start = cursor

        end = start + int(candidate["duration_min"])
        if start < DAY_START or end > DAY_END:
            violations.append(WalkingTimeViolation("DAY_BOUNDARY", day, name, "行程超出当日可用时段"))
        spot = dict(source_spot)
        spot.update(
            start_min=start,
            end_min=end,
            start_time=format_minute(start),
            end_time=format_minute(end),
            period="morning" if start < 720 else "afternoon" if start < 1050 else "evening",
            meal_coverage=meal_coverage_for_visit(str(candidate.get("meal_scene") or "none"), start, end),
            walking_from_previous_min=leg_minutes,
            walking_time_source="amap_walking" if leg_minutes is not None else None,
        )
        spots.append(spot)
        cursor = end
        previous_candidate = candidate
    return {**dict(day_route), "spots": spots}, violations


def recalculate_walking_schedule(route, candidates, walking_minutes, *, travel_start_date=None):
    """Recompute each stop from authoritative walking time and hard time bounds."""
    candidate_map = {str(item["poi_name"]): item for item in candidates}
    updated, violations = [], []
    for day_route in route:
        day = int(day_route.get("day", 0))
        visit_date = travel_start_date + timedelta(days=day - 1) if travel_start_date and day > 0 else None
        scheduled, day_violations = _scheduled_day(day_route, candidate_map, walking_minutes, visit_date)
        updated.append(scheduled)
        violations.extend(day_violations)
    status = "verified" if not violations else "conflict"
    return WalkingScheduleCheck(status, updated, tuple(violations))


def _legal_alternate_orders(names, candidates):
    if len(names) > 5:
        return []
    valid = []
    for order in itertools.permutations(names):
        if tuple(names) == order:
            continue
        positions = {name: index for index, name in enumerate(order)}
        if any(candidates[name].get("must_be_first") and positions[name] != 0 for name in order):
            continue
        if any(candidates[name].get("must_be_last") and positions[name] != len(order) - 1 for name in order):
            continue
        if any(
            positions[name] >= positions[successor]
            for name in order
            for successor in candidates[name].get("before_poi_names") or []
            if successor in positions
        ):
            continue
        valid.append(order)
    return valid


def _approximate_distance(order, candidates):
    return sum(haversine_km(candidates[a]["location"], candidates[b]["location"]) for a, b in zip(order, order[1:]))


def _walking_minutes_by_pair(minutes: Mapping[str, Mapping[str, int]]) -> dict[tuple[str, str], int]:
    return {(origin, destination): value for origin, targets in minutes.items() for destination, value in targets.items()}


def _walking_minutes_by_identity(minutes: Mapping[tuple[str, str], int]) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {}
    for (origin, destination), value in minutes.items():
        result.setdefault(origin, {})[destination] = value
    return result


async def verify_walking_schedule(
    route: Sequence[Mapping],
    candidates: Sequence[Mapping],
    provider: WalkingLegProvider,
    *,
    travel_start_date: date | None = None,
    max_corrections_per_day: int = 2,
    walking_time_minutes: Mapping[str, Mapping[str, int]] | None = None,
    max_new_queries: int = 60,
    max_concurrent_requests: int = 4,
) -> WalkingScheduleCheck:
    """Verify a route, reusing measured directed legs within a bounded request budget."""
    candidate_map = {str(item["poi_name"]): item for item in candidates}
    cache = _walking_minutes_by_pair(walking_time_minutes or {})
    queried: set[tuple[str, str]] = set()
    semaphore = asyncio.Semaphore(max(1, max_concurrent_requests))

    def checked(working_route, status, violations, corrections):
        return WalkingScheduleCheck(
            status, working_route, tuple(violations), corrections,
            _walking_minutes_by_identity(cache), len(queried),
        )

    async def query_day(day_route):
        day = int(day_route.get("day", 0))
        names = [str(spot.get("name") or "") for spot in day_route.get("spots") or []]
        missing = []
        for origin_name, destination_name in zip(names, names[1:]):
            origin, destination = candidate_map.get(origin_name), candidate_map.get(destination_name)
            if origin is None or destination is None:
                return WalkingTimeUnavailable("WALKING_TIME_MISSING", day, origin_name, destination_name)
            key = (walking_place_key(origin), walking_place_key(destination))
            for legacy_key in (
                (f"name:{origin_name.casefold()}", f"name:{destination_name.casefold()}"),
                (origin_name, destination_name),
            ):
                if key not in cache and legacy_key in cache:
                    cache[key] = cache[legacy_key]
            if key in cache:
                continue
            missing.append((key, origin, destination, origin_name, destination_name))
        if len(queried) + len(missing) > max_new_queries:
            origin_name, destination_name = (missing[0][3], missing[0][4]) if missing else ("", "")
            return WalkingTimeUnavailable("WALKING_QUERY_BUDGET_EXCEEDED", day, origin_name, destination_name)

        async def fetch(key, origin, destination, origin_name, destination_name):
            queried.add(key)
            try:
                async with semaphore:
                    minutes = await asyncio.wait_for(provider(origin, destination), timeout=10)
            except (TimeoutError, asyncio.TimeoutError):
                return key, None, "WALKING_TIME_TIMEOUT", origin_name, destination_name
            except ValueError:
                return key, None, "WALKING_TIME_MISSING", origin_name, destination_name
            except Exception:
                return key, None, "WALKING_TIME_UNAVAILABLE", origin_name, destination_name
            if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes <= 0:
                return key, None, "WALKING_TIME_MISSING", origin_name, destination_name
            return key, minutes, None, origin_name, destination_name

        for key, minutes, error, origin_name, destination_name in await asyncio.gather(*(fetch(*item) for item in missing)):
            if minutes is not None:
                cache[key] = minutes
            elif error:
                return WalkingTimeUnavailable(error, day, origin_name, destination_name)
        return None

    working_route = [dict(day, spots=[dict(spot) for spot in day.get("spots") or []]) for day in route]
    corrections = 0
    for day_index, day_route in enumerate(working_route):
        unavailable = await query_day(day_route)
        if unavailable:
            error = WalkingTimeViolation(unavailable.code, unavailable.day, unavailable.destination, str(unavailable))
            status = "budget_exhausted" if unavailable.code == "WALKING_QUERY_BUDGET_EXCEEDED" else "unverified"
            return checked(working_route, status, (error,), corrections)

        day_no = int(day_route.get("day", 0))
        visit_date = travel_start_date + timedelta(days=day_no - 1) if travel_start_date and day_no > 0 else None
        scheduled, conflicts = _scheduled_day(day_route, candidate_map, cache, visit_date)
        if not conflicts:
            working_route[day_index] = scheduled
            continue

        names = [str(spot.get("name") or "") for spot in day_route.get("spots") or []]
        alternates = sorted(
            _legal_alternate_orders(names, candidate_map),
            key=lambda order: (_approximate_distance(order, candidate_map), order),
        )[:max(0, max_corrections_per_day)]
        corrected = None
        last_conflicts = conflicts
        for order in alternates:
            alternate = {
                **day_route,
                "spots": [dict(next(spot for spot in day_route["spots"] if spot["name"] == name)) for name in order],
            }
            unavailable = await query_day(alternate)
            corrections += 1
            if unavailable:
                error = WalkingTimeViolation(unavailable.code, unavailable.day, unavailable.destination, str(unavailable))
                return checked(working_route, "unverified", (error,), corrections)
            alternate_schedule, alternate_conflicts = _scheduled_day(alternate, candidate_map, cache, visit_date)
            last_conflicts = alternate_conflicts
            if not alternate_conflicts:
                corrected = alternate_schedule
                break
        if corrected is None:
            working_route[day_index] = scheduled
            all_conflicts = tuple(dict.fromkeys([*conflicts, *last_conflicts]))
            return checked(working_route, "conflict", all_conflicts, corrections)
        working_route[day_index] = corrected

    return checked(working_route, "verified", (), corrections)

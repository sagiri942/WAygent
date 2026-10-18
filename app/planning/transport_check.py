"""Generation-time transport validation; no post-save refresh or scheduled work."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
import json
import math

from app.core.transport import TransportOption, TransportUnavailable, allowed_transport_modes, yuan_cents
from app.planning.time_validation import recalculate_walking_schedule
from app.planning.walking_identity import walking_place_key
from app.providers.amap.transport import query_transport_options
from app.core.transport import choose_transport


def transport_draft_leg(origin, destination, allowed):
    from app.planning.helpers import haversine_km
    speed = 5 if allowed == {"walking"} else 80
    return max(1, math.ceil(haversine_km(origin["location"], destination["location"]) * 60 / speed))


def new_transport_query():
    return {"transport_cache": {}, "transport_time_minutes": {}, "transport_cost_cents": {},
            "transport_requests": 0, "transport_query_date": None, "transport_verification": None,
            "transport_summary": None, "transport_failed_routes": []}


def transport_draft_minutes(state):
    allowed = allowed_transport_modes(state.effective_constraints, state.allowed_transport_modes)
    draft = {}
    for origin in state.candidate_pool:
        source = walking_place_key(origin)
        draft[source] = {}
        for destination in state.candidate_pool:
            if origin == destination:
                continue
            target = walking_place_key(destination)
            draft[source][target] = state.transport_time_minutes.get(source, {}).get(target,
                transport_draft_leg(origin, destination, allowed))
    return draft


def make_transport_check_node(provider=None):
    provider = provider or query_transport_options

    async def check(state):
        if not state.route or not (state.quality_report or {}).get("passed"):
            return {}
        cache = dict(state.transport_cache)
        requests = state.transport_requests
        query_date = date.fromisoformat(state.transport_query_date) if state.transport_query_date else (
            datetime.now(timezone(timedelta(hours=8))).date())
        route = state.route
        projection = {key: dict(value) for key, value in state.transport_time_minutes.items()}
        costs = {key: dict(value) for key, value in state.transport_cost_cents.items()}
        original = state.walking_repair_original_route or state.route
        failed_routes = list(state.transport_failed_routes)
        warnings = []
        semaphore = asyncio.Semaphore(4)

        def result(status, *, violations=None, summary=None):
            verification = {"kind": "transport", "status": status, "verified": status == "verified",
                            "violations": violations or [], "warnings": warnings,
                            "query_date": query_date.isoformat(), "requests": requests}
            coverage = [dict(entry) for entry in state.constraint_coverage]
            if status in {"verified", "conflict"}:
                ids = {str(c.get("id") or c.get("fact_id") or "") for c in state.effective_constraints
                       if c.get("category") == "transport_preference"}
                for entry in coverage:
                    if str(entry.get("constraint_id") or "") in ids:
                        entry.update(status="applied", stages=["transport_check", "quality_gate"])
            before = {spot["name"]: day["day"] for day in original for spot in day["spots"]}
            after = {spot["name"]: day["day"] for day in route for spot in day["spots"]}
            changes = [f"移除{name}（原第{before[name]}天）" for name in sorted(before.keys() - after.keys())]
            changes += [f"加入{name}（第{after[name]}天）" for name in sorted(after.keys() - before.keys())]
            changes += [f"{name}从第{before[name]}天调整到第{after[name]}天" for name in sorted(before.keys() & after.keys()) if before[name] != after[name]]
            return {"route": route, "walking_repair_summary": changes, "transport_cache": cache, "transport_requests": requests,
                    "transport_query_date": query_date.isoformat(), "transport_time_minutes": projection,
                    "transport_cost_cents": costs,
                    "transport_verification": verification, "walking_time_verification": verification,
                    "transport_summary": summary, "walking_repair_original_route": original,
                    "transport_failed_routes": failed_routes, "constraint_coverage": coverage,
                    "approved": status == "verified"}

        try:
            from app.core.planning_brief import required_brief_fields
            if {"transport_budget", "trip_budget"} & set(required_brief_fields(state.model_dump())):
                raise ValueError("请先补充有效的景点间交通预算，且不得超过旅行总预算")
            allowed = allowed_transport_modes(state.effective_constraints, state.allowed_transport_modes)
            budget = yuan_cents(state.transport_budget)
        except ValueError as exc:
            return result("unverified", violations=[{"code": "TRANSPORT_POLICY", "message": str(exc)}])
        modes = [mode for mode in ("walking", "taxi") if mode in allowed]
        if allowed & {"bus", "subway"}:
            modes.append("transit")
        candidates = {item["poi_name"]: item for item in state.candidate_pool}

        async def before_request():
            nonlocal requests
            if requests >= 120:
                raise TransportUnavailable("交通请求预算耗尽")
            requests += 1

        def cache_key(origin, destination, mode, departure):
            return json.dumps(["amap-v5", walking_place_key(origin), walking_place_key(destination), mode,
                query_date.isoformat(), departure if mode == "transit" else None, state.destination,
                sorted(allowed), state.taxi_wait_min, state.taxi_boarding_min], ensure_ascii=False)

        async def options_for(origin, destination, mode, departure):
            key = cache_key(origin, destination, mode, departure)
            if key not in cache:
                try:
                    async with semaphore:
                        if provider is not query_transport_options:
                            await before_request()
                        values = await asyncio.wait_for(provider(origin, destination, mode=mode,
                            city=state.destination, departure_min=departure, query_date=query_date,
                            allowed_modes=allowed, taxi_wait_min=state.taxi_wait_min,
                            taxi_boarding_min=state.taxi_boarding_min, before_request=before_request), timeout=10)
                    options = [value if isinstance(value, TransportOption) else TransportOption.model_validate(value)
                               for value in values]
                    options = [value for value in options if
                               (value.mode in allowed or value.mode == "transit") and
                               all(part.get("mode") in allowed | {"walking"} for part in value.segments)]
                    if not options:
                        raise TransportUnavailable("没有允许且完整的交通方案")
                    cache[key] = {"options": [option.model_dump() for option in options]}
                except Exception as exc:
                    # Never retain request URLs, supplier response text or secrets.
                    cache[key] = {"error": "budget_exhausted" if requests >= 120 else "unavailable"}
            item = cache[key]
            if item.get("error"):
                warnings.append({"origin": origin["poi_name"], "destination": destination["poi_name"],
                                 "mode": mode, "status": item["error"]})
            options = [TransportOption.model_validate(value) for value in item.get("options", [])]
            known = [option.cost_cents for option in options if option.cost_cents is not None]
            if known:
                targets = costs.setdefault(walking_place_key(origin), {})
                target = walking_place_key(destination)
                targets[target] = min([*known, targets.get(target, min(known))])
            return options

        async def warm_reverse(legs):
            reverse_choices = await asyncio.gather(*(
                asyncio.gather(*(options_for(destination, origin, mode, departure) for mode in modes))
                for _, _, origin, destination, departure in legs))
            for leg, values in zip(legs, reverse_choices):
                _, _, origin, destination, _ = leg
                options = [option for group in values for option in group
                           if budget is None or option.cost_cents is not None and option.cost_cents <= budget]
                if options:
                    projection.setdefault(walking_place_key(destination), {})[walking_place_key(origin)] = min(option.minutes for option in options)

        for _pass in range(3):
            warnings.clear()
            legs = []
            for day_index, day in enumerate(route):
                for spot_index, (left, right) in enumerate(zip(day["spots"], day["spots"][1:]), 1):
                    if left["name"] not in candidates or right["name"] not in candidates:
                        return result("unverified", violations=[{"code": "UNKNOWN_POI", "message": "地点缺少权威坐标"}])
                    legs.append((day_index, spot_index, candidates[left["name"]], candidates[right["name"]], int(left["end_min"])))
            choices = await asyncio.gather(*(
                asyncio.gather(*(options_for(origin, destination, mode, departure) for mode in modes))
                for _, _, origin, destination, departure in legs))
            flattened = [[option for mode_options in values for option in mode_options] for values in choices]
            if any(not values for values in flattened):
                return result("budget_exhausted" if requests >= 120 else "unverified",
                              violations=[{"code": "TRANSPORT_MISSING", "message": "有景点间路段没有完整可用交通时间"}])
            try:
                selected = choose_transport(flattened, budget_cents=budget)
            except TransportUnavailable as exc:
                if any(not any(option.cost_cents is not None for option in values) for values in flattened):
                    return result("unverified", violations=[{"code": "TRANSPORT_FARE_MISSING", "message": str(exc)}])
                await warm_reverse(legs)
                return result("conflict", violations=[{"code": "TRANSPORT_BUDGET", "message": str(exc)}])
            updated, conflicts = [], []
            by_day = {}
            for leg, option in zip(legs, selected):
                day_index, spot_index, origin, destination, departure = leg
                source_key, target_key = walking_place_key(origin), walking_place_key(destination)
                by_day.setdefault(day_index, {}).setdefault(source_key, {})[target_key] = option.minutes
                projection.setdefault(source_key, {})[target_key] = option.minutes
            for day_index, day in enumerate(route):
                day_minutes = {(origin, target): minutes for origin, targets in by_day.get(day_index, {}).items()
                               for target, minutes in targets.items()}
                scheduled = recalculate_walking_schedule([day], state.candidate_pool, day_minutes,
                                                        travel_start_date=state.travel_start_date)
                updated.extend(scheduled.route)
                conflicts.extend({**item.__dict__, "message": item.message.replace("真实步行", "预计交通").replace("步行", "交通")} for item in scheduled.violations)
            for leg, option in zip(legs, selected):
                day_index, spot_index, origin, destination, departure = leg
                spot = updated[day_index]["spots"][spot_index]
                spot["transport_from_previous"] = {**option.model_dump(), "origin": origin["poi_name"],
                    "destination": destination["poi_name"], "departure_time": f"{departure // 60:02d}:{departure % 60:02d}",
                    "query_date": query_date.isoformat(), "comparison_scope": "available_options"}
                spot.pop("walking_from_previous_min", None)
                spot.pop("walking_time_source", None)
            stable = all(departure == updated[day_index]["spots"][spot_index - 1]["end_min"]
                         for day_index, spot_index, _, _, departure in legs)
            route = updated
            if conflicts:
                # Resolve reverse edges of the current route before another
                # solve. Otherwise an optimistic unknown reverse direction can
                # consume each repair round by merely flipping the same pair.
                await warm_reverse(legs)
                fingerprint = json.dumps([[day["day"], [spot["name"] for spot in day["spots"]]] for day in route])
                if fingerprint in failed_routes:
                    return result("repair_no_feasible_solution", violations=conflicts)
                failed_routes.append(fingerprint)
                return result("conflict", violations=conflicts)
            if stable:
                return result("verified", summary={"scope": "attraction_legs_only", "query_date": query_date.isoformat(),
                    "minutes": sum(option.minutes for option in selected),
                    "cost_cents": sum(option.cost_cents for option in selected) if all(option.cost_cents is not None for option in selected) else None,
                    "budget_cents": budget, "warnings": warnings})
        return result("unverified", violations=[{"code": "TRANSPORT_TIME_UNSTABLE", "message": "本次规划内交通时刻尚未收敛"}])
    return check

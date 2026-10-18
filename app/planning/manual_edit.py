"""Reconcile an explicit manual edit with the checkpoint used by AI revisions.

Coordinates and candidate semantics remain server owned. This does not claim
that a manually timed route has passed walking or optimizer quality checks.
"""
from __future__ import annotations

import copy
import re

from app.core.daily_counts import daily_bounds_from_constraints
from app.core.planning_constraints import compatibility_preferences
from app.planning.candidate_builder import build_authoritative_candidates, candidate_pool_fingerprint, resolve_constraint_coverage
from app.planning.poi_resolution import unique_named_poi
from app.planning.schemas import CandidateAttraction
from app.planning.optimizer import meal_coverage_for_visit


class ManualEditRejected(ValueError):
    pass


def _names(day):
    return [item["name"] for item in day.get("timeline", []) if item.get("type") == "attraction"]


def _editable_timeline(day):
    return [{key: value for key, value in item.items() if key not in {"meal_coverage", "dist_from_prev_km"}}
            for item in day.get("timeline", [])]


def _minute(value):
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", str(value or "")):
        raise ManualEditRejected("景点时间格式必须为 HH:MM")
    hour, minute = str(value).split(":")
    return int(hour) * 60 + int(minute)


def reconcile_manual_edit(plan, previous_plan, planner_state, *, search_pois):
    """Return a synchronized copy, or reject without persisting either JSON."""
    state = copy.deepcopy(planner_state)
    previous_days = {day["day"]: day for day in previous_plan["days"]}
    routes = {day["day"]: day for day in state.get("route", [])}
    changed = [day for day in plan["days"] if (
        _editable_timeline(day) != _editable_timeline(previous_days[day["day"]])
        or _names(day) != [spot.get("name") for spot in routes.get(day["day"], {}).get("spots", [])]
    )]
    if not changed:
        return state
    pois = copy.deepcopy(state.get("pois") or [])
    poi_by_name = {poi["name"]: poi for poi in pois}
    old_pool = {item["poi_name"]: item for item in state.get("candidate_pool", [])}
    proposals = {name: {key: value for key, value in item.items() if key in CandidateAttraction.model_fields}
                 for name, item in old_pool.items()}
    selected = [name for day in plan["days"] for name in _names(day)]
    if len(selected) != len(set(selected)):
        raise ManualEditRejected("同一地点不能重复安排，请检查各天景点")
    removed = {name for day in changed for name in (
        _names(previous_days[day["day"]]) + [spot.get("name") for spot in routes.get(day["day"], {}).get("spots", [])]
    ) if name and name not in selected}
    moved = {name for day in changed for name in _names(day)}
    previous_day_by_name = {spot.get("name"): day["day"] for day in state.get("route", []) for spot in day.get("spots", [])}
    changed_day_by_name = {name: day["day"] for day in changed for name in _names(day)}
    changed_date = {name for name in moved if name in previous_day_by_name and previous_day_by_name[name] != changed_day_by_name[name]}
    constraints = copy.deepcopy(state.get("effective_constraints") or plan.get("effective_constraints") or [])
    # Only complete single-place requirements are superseded by this explicit
    # edit. Appointments, compound requirements and precedence remain hard.
    retained = []
    for item in constraints:
        value = str(item.get("value_text") or "")
        if item.get("polarity") == "require":
            if item.get("category") == "attraction_preference":
                if any(re.fullmatch(rf"(?:第\s*\d+\s*天)?(?:必须去|一定要去|必去){re.escape(name)}", value) for name in removed):
                    continue
                if any(re.fullmatch(rf"第\s*\d+\s*天(?:必须)?去{re.escape(name)}", value) for name in changed_date):
                    continue
            if item.get("category") == "schedule_preference" and any(
                re.fullmatch(rf"第\s*\d+\s*天(?:必须)?去{re.escape(name)}", value) for name in moved | removed
            ):
                continue
        if item.get("polarity") == "avoid" and item.get("source") in {"explicit_manual_revision", "explicit_local_revision"} and any(
            value == f"不要去{name}" for name in moved
        ):
            continue
        retained.append(item)
    constraints = retained
    for name in sorted(removed):
        constraints.append(dict(id=f"manual-avoid-{name}", category="attraction_preference", polarity="avoid",
                                value_text=f"不要去{name}", source="explicit_manual_revision"))
    searches = 0
    for day in changed:
        spots = []
        previous_end = None
        for item in day["timeline"]:
            if item.get("type") != "attraction":
                continue
            name = item["name"]
            start, end = _minute(item.get("start_time")), _minute(item.get("end_time"))
            if not 30 <= end - start <= 360 or (previous_end is not None and start < previous_end):
                raise ManualEditRejected("景点需按时间排序，每次游览为30至360分钟且不能重叠")
            previous_end = end
            poi = poi_by_name.get(name)
            if poi is None or (item.get("poi_id") and str(poi.get("id")) != str(item["poi_id"])):
                searches += 1
                if searches > 16:
                    raise ManualEditRejected("一次最多核验16个新地点，请分批保存")
                poi = unique_named_poi(search_pois(name), name, poi_id=item.get("poi_id"))
                if poi is None:
                    raise ManualEditRejected(f"无法核验地点“{name}”，请从搜索结果选择完整名称")
                pois = [old for old in pois if old.get("name") != name] + [poi]
                poi_by_name[name] = poi
            if not poi.get("id") or not poi.get("location"):
                raise ManualEditRejected(f"地点“{name}”缺少权威坐标，请重新搜索选择")
            for field in ("location", "open_time", "rating"):
                item[field] = copy.deepcopy(poi.get(field))
            item["poi_id"] = poi["id"]
            proposal = proposals.setdefault(name, {"poi_name": name})
            proposal["duration_min"] = end - start
            item["meal_scene"] = proposal.get("meal_scene", "none")
            item["meal_coverage"] = meal_coverage_for_visit(item["meal_scene"], start, end)
            spots.append({key: item[key] for key in ("name", "start_time", "end_time", "period") if key in item})
            for category, value, suffix in (
                ("attraction_preference", f"必须去{name}", "require"),
                ("schedule_preference", f"第{day['day']}天去{name}", "day"),
            ):
                entry = dict(id=f"manual-{suffix}-{name}", category=category, polarity="require",
                             value_text=value, source="explicit_manual_revision")
                constraints = [old for old in constraints if old.get("id") != entry["id"]]
                constraints.append(entry)
        if not spots:
            raise ManualEditRejected("每天至少保留一个景点")
        routes[day["day"]] = {**routes.get(day["day"], {}), "day": day["day"], "spots": spots, "theme": day.get("theme", "")}
    days_count = int(state.get("days") or len(plan["days"]))
    candidates, _ = build_authoritative_candidates(list(proposals.values()), pois, days=days_count, constraints=constraints)
    pool = [item.model_dump(mode="json") for item in candidates]
    # Preserve frozen semantic scheduling rules on retained stops. Recomputed
    # constraint flags must not resurrect a removed single-place requirement.
    for item in pool:
        original = old_pool.get(item["poi_name"], {})
        if item["poi_name"] in selected and original.get("must_visit"):
            item["must_visit"] = True
        for field in ("fixed_start_min", "must_be_first", "must_be_last", "before_poi_names"):
            if original.get(field):
                item[field] = copy.deepcopy(original[field])
    selected_days = {name: day["day"] for day in plan["days"] for name in _names(day)}
    for name in removed:
        if any(old_pool.get(name, {}).get(field) for field in ("fixed_start_min", "must_be_first", "must_be_last", "before_poi_names")):
            raise ManualEditRejected(f"编辑与“{name}”的预约或先后要求冲突")
    if any(name in (item.get("before_poi_names") or []) for item in old_pool.values() for name in removed):
        raise ManualEditRejected("编辑与景点先后要求冲突，请先修改完整要求")
    for item in pool:
        name = item["poi_name"]
        if item.get("must_visit") and name not in selected_days:
            raise ManualEditRejected(f"编辑与必去要求“{name}”冲突，请先修改完整要求")
        if name in selected_days and item.get("fixed_day") not in (None, selected_days[name]):
            raise ManualEditRejected(f"编辑与“{name}”的指定日期冲突")
    minimum, maximum = daily_bounds_from_constraints(constraints)
    for day in changed:
        if (minimum is not None and len(_names(day)) < minimum) or (maximum is not None and len(_names(day)) > maximum):
            raise ManualEditRejected("编辑与每天景点数量要求冲突")
        names = _names(day)
        by_name = {item["poi_name"]: item for item in pool}
        for index, name in enumerate(names):
            item = by_name[name]
            start = _minute(next(spot["start_time"] for spot in day["timeline"] if spot.get("type") == "attraction" and spot["name"] == name))
            if (item.get("fixed_start_min") is not None and start != item["fixed_start_min"]
                    or item.get("must_be_first") and index != 0
                    or item.get("must_be_last") and index != len(names) - 1
                    or any(other not in names[index + 1:] for other in item.get("before_poi_names", []))):
                raise ManualEditRejected("编辑与景点预约或先后要求冲突")
    old_coverage = {entry.get("constraint_id"): entry for entry in state.get("constraint_coverage", [])}
    coverage = resolve_constraint_coverage(constraints, [old_coverage.get(item.get("id"), {"constraint_id": item.get("id"), "status": "unverified"}) for item in constraints], pois, candidates, days=days_count)
    hard_ids = {item.get("id") for item in constraints if item.get("polarity") in {"require", "avoid"} and item.get("category") in {"attraction_preference", "schedule_preference"}}
    if any(entry.get("constraint_id") in hard_ids and entry.get("status") != "applied" for entry in coverage):
        raise ManualEditRejected("编辑后的地点不能满足完整硬性要求，请先修改要求")
    state.update(route=[routes[day["day"]] for day in plan["days"]], pois=pois, candidate_pool=pool,
                 effective_constraints=constraints, constraint_coverage=coverage, approved=False)
    state["candidate_pool_fingerprint"] = candidate_pool_fingerprint(pool)
    state.setdefault("travel_start_date", plan.get("start_date"))
    plan["effective_constraints"] = copy.deepcopy(constraints)
    plan["constraint_coverage"] = copy.deepcopy(coverage)
    plan["preferences"] = compatibility_preferences(constraints)
    state.update(plan["preferences"])
    quality = {"passed": False, "validation_scope": {"kind": "manual_edit", "days": [day["day"] for day in changed]},
               "manual_edit_authority_verified": True, "walking_time_verification": {"status": "unverified"}}
    # A verified original base (or an earlier reconciled manual base) is needed
    # before later AI revisions can use this authority-only checkpoint.
    prior = previous_plan.get("quality_report") or {}
    quality["manual_edit_base_verified"] = prior.get("passed") is True or prior.get("manual_edit_base_verified") is True
    for target in (plan, state):
        target.pop("transport_summary", None)
        target["transport_verification"] = {"status": "unverified", "reason": "manual_edit"}
        for day in (target.get("route", []) if target is state else target.get("days", [])):
            if day["day"] in {changed_day["day"] for changed_day in changed}:
                for spot in day.get("timeline", day.get("spots", [])):
                    spot.pop("transport_from_previous", None)
        target["quality_report"] = copy.deepcopy(quality)
        target["walking_time_verification"] = {"status": "unverified", "reason": "manual_edit"}
        target.pop("solver_diagnostics", None)
        target.pop("score_breakdown", None)
    if "final_plan" in state:
        state["final_plan"] = copy.deepcopy(plan)
    return state

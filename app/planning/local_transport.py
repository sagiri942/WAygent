"""Validate a local edit against the remaining whole-trip transport allowance."""
from copy import deepcopy

from app.core.transport import yuan_cents
from app.planning.local_reorder import LocalReorderRejected, LocalReorderResult, _assemble_target_timeline
from app.planning.schemas import TravelPlanState
from app.planning.transport_check import make_transport_check_node, new_transport_query


async def validate_local_transport(result, target_day, *, provider=None, reuse_snapshot=False):
    plan, state = deepcopy(result.plan), deepcopy(result.planner_state)
    budget = yuan_cents(state.get("transport_budget"))
    fixed_minutes, fixed_cost, unknown_cost = 0, 0, False
    for day in state["route"]:
        if day["day"] == target_day:
            continue
        for spot in day["spots"][1:]:
            leg = spot.get("transport_from_previous")
            if not leg:
                raise LocalReorderRejected("transport_unverified", "其他日期缺少交通快照，无法核验全程预算；请使用全量修改。")
            fixed_minutes += leg["minutes"]
            if leg.get("cost_cents") is None:
                unknown_cost = True
            else:
                fixed_cost += leg["cost_cents"]
    if budget is not None and (unknown_cost or fixed_cost > budget):
        raise LocalReorderRejected("transport_budget_unverified", "未修改日期的费用不能满足交通预算，原行程保持不变。")
    target = next(day for day in state["route"] if day["day"] == target_day)
    request = {**state, **new_transport_query(), "route": [target],
               "transport_budget": str((budget - fixed_cost) / 100) if budget is not None else None,
               "trip_budget": None, "walking_repair_original_route": [], "quality_report": {"passed": True}}
    if reuse_snapshot:
        request.update({field: state.get(field, default) for field, default in (
            ("transport_cache", {}), ("transport_requests", 0), ("transport_query_date", None))})
    checked = await make_transport_check_node(provider)(TravelPlanState(**request))
    verification = checked["transport_verification"]
    if not verification["verified"]:
        raise LocalReorderRejected("transport_" + verification["status"],
                                   "本次局部安排未通过交通核验，原行程保持不变。", data={"transport_verification": verification,
                                    "transport_cache": checked["transport_cache"], "transport_requests": checked["transport_requests"],
                                    "transport_query_date": checked["transport_query_date"]})
    checked_day = checked["route"][0]
    state["route"] = [checked_day if day["day"] == target_day else day for day in state["route"]]
    target_plan = next(day for day in plan["days"] if day["day"] == target_day)
    target_plan["timeline"] = _assemble_target_timeline(target_plan, checked_day["spots"])
    summary = {**checked["transport_summary"], "minutes": fixed_minutes + checked["transport_summary"]["minutes"],
               "cost_cents": None if unknown_cost or checked["transport_summary"]["cost_cents"] is None else fixed_cost + checked["transport_summary"]["cost_cents"],
               "budget_cents": budget, "query_date": None, "note": "未修改日期保留原查询快照；查询时间见各路段"}
    for value in (state, plan):
        value.update(transport_verification={**verification, "scope": "target_day_only", "target_day": target_day},
                     transport_summary=summary, walking_time_verification=verification)
        value["quality_report"] = {**value.get("quality_report", {}), "walking_time_verification": verification,
                                   "transport_verification": verification}
    state.update(transport_cache=checked["transport_cache"], transport_requests=checked["transport_requests"],
                 transport_query_date=checked["transport_query_date"], transport_time_minutes=checked["transport_time_minutes"])
    return LocalReorderResult(plan, state, verification)

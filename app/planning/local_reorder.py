"""Bounded, same-day itinerary reordering with authoritative walking-time checks."""

from __future__ import annotations

import asyncio
import copy
import itertools
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Mapping, Sequence

from app.planning.helpers import haversine_km
from app.planning.time_validation import recalculate_walking_schedule

MAX_LOCAL_REORDER_STOPS = 5
MAX_LOCAL_REORDER_ORDERS = 12
WALKING_QUERY_TIMEOUT_SECONDS = 10


class LocalReorderRejected(RuntimeError):
    """A user-safe local reorder rejection that must not persist a new version."""

    def __init__(self, code: str, message: str, *, data: dict[str, Any] | None = None):
        self.code = code
        self.public_message = message
        self.data = data or {}
        super().__init__(message)


@dataclass(frozen=True)
class LocalReorderResult:
    plan: dict[str, Any]
    planner_state: dict[str, Any]
    verification: dict[str, Any]


def _day_number(value: str) -> int | None:
    numerals = {
        "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
        "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
    }
    if value.isdigit():
        return int(value)
    if value == "十":
        return 10
    if len(value) == 2 and value[0] == "十":
        return 10 + numerals.get(value[1], 0)
    if len(value) == 2 and value[1] == "十":
        return numerals.get(value[0], 0) * 10
    if len(value) == 3 and value[1] == "十":
        return numerals.get(value[0], 0) * 10 + numerals.get(value[2], 0)
    return numerals.get(value)


def extract_explicit_target_day(modification_notes: str) -> int:
    """Require one explicit numbered day and reject requests that widen scope."""
    text = str(modification_notes or "").strip()
    if re.search(r"每天|所有天|各天|多天|跨日|跨天|全程|整体|全程", text):
        raise LocalReorderRejected(
            "revision_scope_confirmation_required",
            "这次请求涉及多天或整段行程。单日重排不会扩大修改范围，请先确认要修改的具体一天。",
        )
    if re.search(r"增加|新增|删除|移除|替换|换成|改目的地|改日期|改天数|加一个|删一个", text):
        raise LocalReorderRejected(
            "revision_scope_confirmation_required",
            "这次请求包含增删、替换或整体变更，不能作为单日重排执行；原行程没有修改。",
        )
    matches = re.findall(r"(?:第\s*)?([0-9]+|[一二两三四五六七八九十]{1,3})\s*天", text)
    days = [_day_number(item) for item in matches]
    unique_days = {day for day in days if day is not None}
    if len(unique_days) != 1 or any(day is None or day < 1 for day in days):
        if len(unique_days) > 1:
            raise LocalReorderRejected(
                "revision_scope_confirmation_required",
                "请求提到了多天，单日重排无法执行；请指定只调整哪一天。",
            )
        raise LocalReorderRejected(
            "revision_day_ambiguous",
            "请明确要重排第几天的景点顺序，原行程没有修改。",
        )
    return next(iter(unique_days))


_ORDER_REQUEST_MARKERS = re.compile("\u987a\u5e8f|\u91cd\u6392|\u6392\u5e8f|\u91cd\u65b0\u6392\u5217|\u8c03\u6574\u8def\u7ebf|\u5148\u53bb|\u5148\u5230|\u7b2c\u4e00\u7ad9|\u6700\u540e\u4e00\u7ad9|\u538b\u8f74")


def is_explicit_order_request(modification_notes: str) -> bool:
    """Identify only requests that unambiguously ask to change stop order."""
    return bool(_ORDER_REQUEST_MARKERS.search(str(modification_notes or "")))


def _explicit_poi_precedence(
    modification_notes: str, poi_names: Sequence[str]
) -> list[tuple[str, str]]:
    """Extract supported named-stop order constraints from clear user wording."""
    text = str(modification_notes or "")
    unsupported_hard_order = re.search(
        r"\u4e0d\u8981|\u522b|\u4e0d\u80fd|\u4e0d\u5f97|\u4e0d\u8bb8|\u65e0\u9700|\u4e0d\u5fc5|"
        r"\u7b2c\u4e00\u7ad9|\u9996\u7ad9|\u9996\u5148|\u6700\u5148|"
        r"\u7b2c\u4e00\u4e2a|\u6392\u7b2c\u4e00|\u5fc5\u987b|\u52a1\u5fc5|\u4e00\u5b9a|"
        r"\u6700\u540e\u4e00\u7ad9|\u672b\u7ad9|\u538b\u8f74",
        text,
    )
    if unsupported_hard_order:
        raise LocalReorderRejected(
            "revision_order_requirement_unresolved",
            "这条请求包含当前局部重排无法完整验证的顺序或否定要求，请重新表达为单一明确的景点先后关系；原版本没有修改。",
        )
    names = sorted(set(poi_names), key=lambda name: (-len(name), name))
    precedences: set[tuple[str, str]] = set()
    for first in names:
        for second in names:
            if first == second:
                continue
            first_then_second = re.search(
                re.escape(first) + r".{0,20}(?:再|然后|接着).{0,6}" + re.escape(second), text
            )
            first_before_second = re.search(
                re.escape(first) + r"(?:在|排在|安排在)?" + re.escape(second)
                + r"(?:之前|前面|前)", text
            ) or re.search(
                re.escape(first) + r"(?:要)?(?:在)?(?:到)?(?:于)?"
                + r"(?:之前|前面|前)" + re.escape(second), text
            )
            if first_then_second or first_before_second:
                precedences.add((first, second))
    paired_names = {name for pair in precedences for name in pair}
    standalone_firsts = {
        name for name in names
        if re.search(
            r"(?:\u5148|\u4f18\u5148)(?:\u53bb|\u5230|\u5b89\u6392)?\s*" + re.escape(name),
            text,
        )
    }
    if standalone_firsts - paired_names:
        raise LocalReorderRejected(
            "revision_order_requirement_unresolved",
            "请求指定了首站，但当前局部重排无法安全处理单独首站要求；请明确为成对的景点先后关系，原版本没有修改。",
        )
    if precedences:
        return sorted(precedences)
    if re.search("\u5fc5\u987b\u5148|\u5148\u53bb|\u5148\u5230|\u7b2c\u4e00\u7ad9|\u6700\u540e\u4e00\u7ad9|\u538b\u8f74|\u4e4b\u524d|\u4e4b\u540e", text):
        raise LocalReorderRejected(
            "revision_order_requirement_unresolved",
            "我无法把指定顺序准确对应到当天已保存的景点，请确认地点名称或顺序；原版本没有修改。",
        )
    return []


def _reject_unconsumed_hard_constraints(modification_notes: str) -> None:
    text = str(modification_notes or "")
    unsupported = re.search(
        r"\d{1,2}\s*[:：]\s*\d{2}|\d{1,2}\s*\u70b9|"
        r"\u9884\u7ea6|\u51e0\u70b9|\u6700\u65e9|\u6700\u665a|"
        r"\u6bcf\u5929.{0,12}(?:\u53ea|\u4ec5|\u81f3\u591a|\u4e0d\u8d85\u8fc7|\u81f3\u5c11|\u81f3\u591a).{0,12}(?:\u666f\u70b9|\u5730\u70b9|\u4e2a)|"
        r"\u6bcf\u4e00\u5929.{0,12}(?:\u53ea|\u4ec5|\u81f3\u591a|\u4e0d\u8d85\u8fc7|\u81f3\u5c11).{0,12}(?:\u666f\u70b9|\u5730\u70b9|\u4e2a)|"
        r"\d+\s*(?:\u4e2a|\u5904)\s*(?:\u666f\u70b9|\u5730\u70b9|\u666f\u533a)",
        text,
    )
    if unsupported:
        raise LocalReorderRejected(
            "revision_scope_confirmation_required",
            "请求还包含局部重排当前不能完整执行的时间、预约或景点数量要求；请拆分请求，原版本没有修改。",
        )


def _attractions(day: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        dict(item) for item in day.get("timeline") or []
        if isinstance(item, Mapping) and item.get("type") == "attraction"
    ]


def _time_minute(value: Any) -> int | None:
    if not isinstance(value, str) or not re.fullmatch(r"\d{2}:\d{2}", value):
        return None
    hour, minute = map(int, value.split(":"))
    return hour * 60 + minute if hour < 24 and minute < 60 else None


def _meal_anchor(spots: Sequence[Mapping[str, Any]], kind: str) -> str | None:
    window = (690, 810) if kind == "lunch" else (1050, 1200)
    best: tuple[int, int, str] | None = None
    for index, spot in enumerate(spots):
        start, end = _time_minute(spot.get("start_time")), _time_minute(spot.get("end_time"))
        if start is None or end is None:
            continue
        overlap = max(0, min(end, window[1]) - max(start, window[0]))
        key = (overlap, -index, str(spot.get("name") or ""))
        if best is None or key > best:
            best = key
    if best and best[0] > 0:
        return best[2]
    later = next(
        (str(spot.get("name")) for spot in spots
         if (_time_minute(spot.get("start_time")) or 0) >= window[0]),
        None,
    )
    return later or (str(spots[-1].get("name")) if spots else None)


def _assemble_target_timeline(
    original_day: Mapping[str, Any], scheduled_spots: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    originals = {str(item.get("name")): dict(item) for item in _attractions(original_day)}
    updated_attractions: list[dict[str, Any]] = []
    for scheduled in scheduled_spots:
        name = str(scheduled.get("name") or "")
        if name not in originals:
            raise LocalReorderRejected(
                "local_reorder_conflict", "重排结果包含原行程没有的地点，未保存新版本。"
            )
        updated = originals[name]
        for field in (
            "start_time", "end_time", "period", "meal_coverage",
            "walking_from_previous_min", "walking_time_source",
            "transport_from_previous",
        ):
            updated[field] = scheduled.get(field)
        updated_attractions.append(updated)

    anchor_by_item: dict[int, str | None] = {}
    timeline = list(original_day.get("timeline") or [])
    previous_attraction: str | None = None
    for index, item in enumerate(timeline):
        if item.get("type") == "attraction":
            previous_attraction = str(item.get("name") or "")
            continue
        if item.get("type") in {"lunch", "dinner"}:
            anchor_by_item[index] = _meal_anchor(
                updated_attractions, str(item.get("type"))
            )
        else:
            anchor_by_item[index] = previous_attraction

    after: dict[str, list[dict[str, Any]]] = {}
    leading: list[dict[str, Any]] = []
    for index, item in enumerate(timeline):
        if item.get("type") == "attraction":
            continue
        copied = copy.deepcopy(item)
        anchor = anchor_by_item.get(index)
        if anchor in originals:
            after.setdefault(str(anchor), []).append(copied)
        else:
            leading.append(copied)

    result = list(leading)
    for spot in updated_attractions:
        result.append(spot)
        result.extend(after.get(str(spot.get("name") or ""), []))
    for values in after.values():
        # If the referenced anchor was removed by malformed legacy data, retain
        # the entry rather than dropping user-authored content.
        if not any(item in result for item in values):
            result.extend(values)
    return result


def _valid_orders(
    names: Sequence[str], candidates: Mapping[str, Mapping[str, Any]], target_day: int
) -> list[tuple[str, ...]]:
    valid = []
    for order in itertools.permutations(names):
        positions = {name: index for index, name in enumerate(order)}
        if any(
            item.get("fixed_day") is not None and int(item["fixed_day"]) != target_day
            for item in candidates.values()
        ):
            raise LocalReorderRejected(
                "local_reorder_conflict",
                "当天景点含有与目标日不一致的指定日期要求；原行程没有修改。",
            )
        if any(item.get("must_be_first") and positions[name] != 0 for name, item in candidates.items()):
            continue
        if any(item.get("must_be_last") and positions[name] != len(order) - 1 for name, item in candidates.items()):
            continue
        if any(
            positions[name] >= positions[successor]
            for name, item in candidates.items()
            for successor in item.get("before_poi_names") or []
            if successor in positions
        ):
            continue
        valid.append(tuple(order))
    return valid


async def reorder_single_day(
    plan: Mapping[str, Any],
    planner_state: Mapping[str, Any],
    *,
    target_day: int,
    modification_notes: str,
    provider,
) -> LocalReorderResult:
    """Optimize only existing target-day attractions and verify every walking leg."""
    parsed_day = extract_explicit_target_day(modification_notes)
    _reject_unconsumed_hard_constraints(modification_notes)
    if parsed_day != target_day:
        raise LocalReorderRejected(
            "revision_day_mismatch", "请求中的目标日期不一致，请重新明确要调整的天数。",
        )
    plan_copy = copy.deepcopy(dict(plan))
    state_copy = copy.deepcopy(dict(planner_state))
    target_plan = next(
        (day for day in plan_copy.get("days") or [] if int(day.get("day") or 0) == target_day),
        None,
    )
    route = list(state_copy.get("route") or [])
    target_route = next(
        (day for day in route if int(day.get("day") or 0) == target_day),
        None,
    )
    quality_report = plan_copy.get("quality_report")
    manual_base = isinstance(quality_report, Mapping) and (
        quality_report.get("manual_edit_authority_verified") is True
        and quality_report.get("manual_edit_base_verified") is True
    )
    if not isinstance(quality_report, Mapping) or (quality_report.get("passed") is not True and not manual_base):
        raise LocalReorderRejected(
            "local_reorder_quality_rejected",
            "基准行程没有通过原质量检查，不能在此基础上保存重排版本。",
            data={"status": "rejected", "reason": "base_quality_report_not_passed"},
        )
    if target_plan is None or target_route is None:
        raise LocalReorderRejected(
            "revision_day_invalid", "找不到指定日期的已保存行程，原版本保持不变。",
        )

    route_spots = [dict(item) for item in target_route.get("spots") or []]
    route_names = [str(item.get("name") or "") for item in route_spots]
    plan_attractions = _attractions(target_plan)
    plan_names = [str(item.get("name") or "") for item in plan_attractions]
    if (
        not route_names or len(route_names) != len(set(route_names))
        or len(plan_names) != len(set(plan_names)) or set(route_names) != set(plan_names)
    ):
        raise LocalReorderRejected(
            "local_reorder_unverified",
            "已保存行程缺少可核验的当天地点数据，无法安全重排；原版本保持不变。",
        )
    if len(route_names) > MAX_LOCAL_REORDER_STOPS:
        raise LocalReorderRejected(
            "local_reorder_search_incomplete",
            f"第{target_day}天景点超过当前可核验范围；原版本保持不变，可缩小范围后重试。",
            data={"status": "unverified", "search_complete": False},
        )

    poi_by_name = {
        str(item.get("name") or ""): dict(item)
        for item in state_copy.get("pois") or [] if item.get("name")
    }
    semantic_by_name = {
        str(item.get("poi_name") or ""): dict(item)
        for item in state_copy.get("candidate_pool") or [] if item.get("poi_name")
    }
    candidates: dict[str, dict[str, Any]] = {}
    for name in route_names:
        poi = poi_by_name.get(name)
        semantic = semantic_by_name.get(name)
        if not poi or not semantic or not poi.get("location"):
            raise LocalReorderRejected(
                "local_reorder_unverified",
                "当天地点缺少冻结的权威坐标或规划语义，无法核验路线；原版本保持不变。",
            )
        candidates[name] = {**poi, **semantic, "name": name, "poi_name": name}
    user_precedence = _explicit_poi_precedence(modification_notes, route_names)
    for first, second in user_precedence:
        candidates[first]["before_poi_names"] = list(dict.fromkeys(
            [*(candidates[first].get("before_poi_names") or []), second]
        ))
        semantic_by_name[first]["before_poi_names"] = list(dict.fromkeys(
            [*(semantic_by_name[first].get("before_poi_names") or []), second]
        ))
    if user_precedence:
        state_copy["candidate_pool"] = [
            semantic_by_name.get(str(item.get("poi_name") or ""), dict(item))
            for item in state_copy.get("candidate_pool") or []
        ]
    orders = _valid_orders(route_names, candidates, target_day)
    total_valid_orders = len(orders)
    if not orders:
        raise LocalReorderRejected(
            "local_reorder_conflict", "当天硬约束之间没有可行的重排顺序；原版本保持不变。",
        )
    orders.sort(key=lambda order: (
        sum(haversine_km(candidates[a]["location"], candidates[b]["location"]) for a, b in zip(order, order[1:])),
        order,
    ))
    orders = orders[:MAX_LOCAL_REORDER_ORDERS]
    all_order_count = total_valid_orders

    start_date_value = state_copy.get("travel_start_date")
    if isinstance(start_date_value, date):
        start_date = start_date_value
    elif start_date_value:
        try:
            start_date = date.fromisoformat(str(start_date_value))
        except ValueError as exc:
            raise LocalReorderRejected(
                "local_reorder_unverified", "基础行程日期无效，无法核验指定日约束；原版本保持不变。",
            ) from exc
    else:
        raise LocalReorderRejected(
            "local_reorder_unverified",
            "基础行程缺少实际出行日期，无法复核闭馆日与天气映射；原版本保持不变。",
        )
    target_date = start_date + timedelta(days=target_day - 1)
    stored_target_date = target_plan.get("date")
    if stored_target_date:
        try:
            stored_target_date = date.fromisoformat(str(stored_target_date))
        except ValueError as exc:
            raise LocalReorderRejected(
                "local_reorder_unverified",
                "基准行程中的目标日日期无效，无法安全复核时间要求；原版本保持不变。",
            ) from exc
        if stored_target_date != target_date:
            raise LocalReorderRejected(
                "local_reorder_unverified",
                "目标日与出行日期范围不一致，无法确认天气和闭馆约束；原版本保持不变。",
            )

    leg_cache: dict[tuple[str, str], int] = {}
    unavailable: list[str] = []
    semaphore = asyncio.Semaphore(4)

    async def get_leg(origin_name: str, destination_name: str) -> int | None:
        key = (origin_name, destination_name)
        if key in leg_cache:
            return leg_cache[key]
        async with semaphore:
            try:
                minutes = await asyncio.wait_for(
                    provider(candidates[origin_name], candidates[destination_name]),
                    timeout=WALKING_QUERY_TIMEOUT_SECONDS,
                )
            except (TimeoutError, asyncio.TimeoutError):
                unavailable.append("WALKING_TIME_TIMEOUT")
                return None
            except Exception:
                unavailable.append("WALKING_TIME_UNAVAILABLE")
                return None
        if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes <= 0:
            unavailable.append("WALKING_TIME_MISSING")
            return None
        leg_cache[key] = minutes
        return minutes

    feasible: list[tuple[int, float, tuple[str, ...], list[dict[str, Any]]]] = []
    conflict_codes: set[str] = set()
    if state_copy.get("transport_enabled"):
        state_copy.update(transport_cache={}, transport_requests=0, transport_query_date=None)
    for order in orders:
        names_for_route = list(order)
        legs = list(zip(names_for_route, names_for_route[1:]))
        values = await asyncio.gather(*(get_leg(*leg) for leg in legs))
        if any(value is None for value in values):
            continue
        route_day = {
            **dict(target_route),
            "spots": [
                {**spot, "name": name}
                for name, spot in zip(
                    names_for_route,
                    [next(item for item in route_spots if item.get("name") == name) for name in names_for_route],
                )
            ],
        }
        schedule = recalculate_walking_schedule(
            [route_day], list(candidates.values()),
            {leg: value for leg, value in zip(legs, values) if value is not None},
            travel_start_date=start_date,
        )
        if not schedule.passed:
            conflict_codes.update(item.code for item in schedule.violations)
            continue
        scheduled_spots = schedule.route[0]["spots"]
        total_walking = sum(
            int(item.get("walking_from_previous_min") or 0) for item in scheduled_spots
        )
        if state_copy.get("transport_enabled"):
            from app.planning.local_transport import validate_local_transport
            trial_plan, trial_state = copy.deepcopy(plan_copy), copy.deepcopy(state_copy)
            next(day for day in trial_plan["days"] if day["day"] == target_day)["timeline"] = _assemble_target_timeline(target_plan, scheduled_spots)
            next(day for day in trial_state["route"] if day["day"] == target_day)["spots"] = scheduled_spots
            # Include authority resolved for newly added/replaced attractions.
            trial_state["candidate_pool"] = [candidates.get(item.get("poi_name"), item) for item in trial_state["candidate_pool"]]
            try:
                checked = await validate_local_transport(LocalReorderResult(trial_plan, trial_state, {}), target_day, reuse_snapshot=True)
            except LocalReorderRejected as exc:
                state_copy.update({key: value for key, value in exc.data.items() if key.startswith("transport_") and key != "transport_verification"})
                conflict_codes.add(exc.code)
                if exc.code.endswith("unverified") or exc.code.endswith("budget_exhausted"):
                    unavailable.append(exc.code)
                continue
            state_copy.update({key: checked.planner_state[key] for key in ("transport_cache", "transport_requests", "transport_query_date")})
            scheduled_spots = next(day for day in checked.planner_state["route"] if day["day"] == target_day)["spots"]
            total_walking = sum(spot.get("transport_from_previous", {}).get("minutes", 0) for spot in scheduled_spots)
        distance = sum(
            haversine_km(candidates[a]["location"], candidates[b]["location"])
            for a, b in zip(order, order[1:])
        )
        feasible.append((total_walking, distance, order, scheduled_spots))

    if not feasible:
        if unavailable:
            raise LocalReorderRejected(
                "walking_time_unverified",
                "步行路线服务暂时无法核验当天完整路线，因此没有保存新版本。",
                data={"status": "unverified", "failure_codes": sorted(set(unavailable))},
            )
        if all_order_count > len(orders):
            raise LocalReorderRejected(
                "local_reorder_search_incomplete",
                "有限重排范围内没有确认可行顺序；原版本保持不变，可澄清约束后重试。",
                data={"status": "unverified", "search_complete": False, "validated_orders": 0},
            )
        raise LocalReorderRejected(
            "walking_time_conflict",
            "当天所有候选顺序都与真实步行时长、开放时间或指定日要求冲突，没有保存新版本。",
            data={"status": "conflict", "violation_codes": sorted(conflict_codes)},
        )

    _, _, _, best_spots = min(feasible, key=lambda item: (item[0], item[1], item[2]))
    target_plan["timeline"] = _assemble_target_timeline(target_plan, best_spots)
    target_route["spots"] = copy.deepcopy(best_spots)
    verification = {
        "status": "verified",
        "verified": True,
        "target_day": target_day,
        "target_date": target_date.isoformat(),
        "weather_forecast_day": (
            target_day
            if any(
                item.get("date") == target_date.isoformat()
                for item in state_copy.get("weather_forecast") or []
                if isinstance(item, Mapping)
            )
            else None
        ),
        "weather_forecast_date": (
            target_date.isoformat()
            if any(
                item.get("date") == target_date.isoformat()
                for item in state_copy.get("weather_forecast") or []
                if isinstance(item, Mapping)
            )
            else None
        ),
        "attempts": len(orders),
        "candidate_orders": all_order_count,
        "search_complete": all_order_count == len(orders),
        "validated_orders": len(feasible),
        "walking_legs_verified": len(leg_cache),
        "violations": [],
        "source": "amap_walking",
        "scope": "target_day_only",
    }
    state_copy["route"] = route
    state_copy["walking_time_verification"] = verification
    state_copy["modification_notes"] = modification_notes
    state_copy["revision_scope"] = {"kind": "local_reorder", "day": target_day}
    plan_copy["walking_time_verification"] = verification
    updated_quality = {
        key: value for key, value in quality_report.items()
        if key not in {"recomputed_objective", "solver_objective_delta", "daily_order_ratios", "manual_edit_authority_verified", "manual_edit_base_verified"}
    }
    updated_quality.update({
        "passed": True, "violations": [],
        "validation_scope": {"kind": "local_reorder", "day": target_day},
        "walking_time_verification": verification,
    })
    plan_copy["quality_report"] = updated_quality
    state_copy["approved"] = True
    state_copy["quality_report"] = updated_quality
    state_copy.pop("solver_diagnostics", None)
    state_copy.pop("score_breakdown", None)
    plan_copy["revision_scope"] = {"kind": "local_reorder", "day": target_day}
    plan_copy.pop("solver_diagnostics", None)
    plan_copy.pop("score_breakdown", None)
    return LocalReorderResult(plan_copy, state_copy, verification)

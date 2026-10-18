"""LangGraph 节点函数：意图识别、景点搜索、规划、评审、时间检查、餐饮搜索、推荐、finalize。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import date, timedelta
from typing import Annotated, Any

from app.core.planning_constraints import constraint_directive

logger = logging.getLogger(__name__)


def _constraints_block(
    state: TravelPlanState, categories: set[str] | None = None
) -> str:
    items = [
        item for item in state.effective_constraints
        if categories is None or item.get("category") in categories
    ]
    if not items:
        return "（无）"
    return "\n".join(
        f"- [{item.get('polarity')}/{item.get('source')}] {constraint_directive(item)}"
        for item in items
    )

from app.llm.factory import build_structured_llm
from app.providers.amap.poi import search_around_pois, search_around_pois_async
from app.planning.candidate_builder import build_authoritative_candidates, candidate_pool_fingerprint, resolve_constraint_coverage
from app.planning.optimizer import (
    AttractionSubsetOptimizer,
    OptimizationFailure,
    daily_bounds_from_constraints,
)
from app.planning.schemas import (
    CandidatePoolProposal,
    DayMealPick,
    IntentExtraction,
    RewrittenQuery,
    RouteReview,
    SingleDayMealPick,
    SpotTipsResult,
    TimeCheckResult,
    TravelPlanState,
    TravelRoute,
)
from app.planning.semantics import has_food_focus, infer_meal_scene, is_featured_food_street
from app.planning.time_validation import verify_walking_schedule
from app.planning.helpers import (
    amap_key,
    clean_pref,
    cluster_pois_by_location,
    dinner_anchor_spot,
    fetch_city_spots,
    fetch_city_spots_async,
    fetch_weather_for_dates,
    fetch_weather_for_dates_async,
    filter_by_rating,
    format_spots_for_llm,
    format_weather_for_llm,
    haversine_km,
    ainvoke_structured,
    last_spot_of_period,
    parse_iso_date,
    restaurant_to_dict,
    spot_location_map,
    unknown_spots,
)
from app.planning.prompts import (
    CANDIDATE_BUILDER_SYSTEM,
    INTENT_SYSTEM,
    MEAL_SYSTEM,
    PLANNER_SYSTEM,
    QUERY_REWRITE_SYSTEM,
    REVIEWER_SYSTEM,
    SPOT_TIPS_SYSTEM,
    TIME_CHECK_SYSTEM,
    WEEKDAYS,
)
from langgraph.graph import END


def _build_planning_llm(schema, model_name: str | None, *, temperature: float = 0):
    """All planning-agent LLM calls use DeepSeek thinking mode explicitly."""
    return build_structured_llm(
        schema,
        provider="deepseek",
        model=model_name or os.getenv("PLANNING_AGENT_MODEL") or None,
        temperature=temperature,
        thinking=True,
        reasoning_effort=os.getenv("PLANNING_AGENT_REASONING_EFFORT", "high"),
    )


def _build_spot_tips_llm(model_name: str | None):
    """Tips are lightweight enrichment, so use one direct structured call."""
    return build_structured_llm(
        SpotTipsResult,
        provider="deepseek",
        model=(
            model_name
            or os.getenv("PLANNING_SPOT_TIPS_MODEL")
            or os.getenv("PLANNING_AGENT_MODEL")
            or None
        ),
        temperature=0,
        thinking=False,
    )


# ─── Query Rewrite Agent ─────────────────────────────────────

def make_query_rewrite_node(model_name: str | None, profile_hint: str = ""):
    """Rewrite from the immutable profile hint carried by the planning Run."""
    rewrite_llm = _build_planning_llm(RewrittenQuery, model_name, temperature=0)

    async def query_rewrite(state: TravelPlanState) -> dict[str, Any]:
        raw = state.query
        try:
            profile_text = profile_hint or state.profile_hint or "（该用户暂无历史画像）"
            if state.effective_constraints:
                profile_text = _constraints_block(state)

            intent_prefs = (
                f"本次查询提取的偏好：景点={state.attraction_preference or '无'}，"
                f"餐饮={state.food_preference or '无'}，习惯={state.habit_preference or '无'}"
            )
            rewritten: RewrittenQuery = await ainvoke_structured(rewrite_llm, [
                ("system", QUERY_REWRITE_SYSTEM),
                ("human", f"原始查询：{raw}\n\n{intent_prefs}\n\n用户历史画像：\n{profile_text}"),
            ])

            note = f"[query_rewrite] {raw!r} → {rewritten.rewritten_query!r}（{rewritten.reasoning}）"
            return {
                "rewritten_query": rewritten.rewritten_query,
                "attraction_preference": clean_pref(rewritten.attraction_preference) or state.attraction_preference,
                "food_preference":       clean_pref(rewritten.food_preference)       or state.food_preference,
                "habit_preference":      clean_pref(rewritten.habit_preference)      or state.habit_preference,
                "history": state.history + [note],
            }
        except Exception as exc:
            # 降级：直接用原始 query，不中断主流程；偏好字段保持 intent 提取值
            return {
                "history": state.history + [f"[query_rewrite] 失败降级（{exc}），使用原始查询"],
            }

    return query_rewrite


# ─── 意图识别 ────────────────────────────────────────────────

def make_intent_node(model_name: str | None, profile_hint: str = ""):
    llm = _build_planning_llm(IntentExtraction, model_name, temperature=0)

    async def intent(state: TravelPlanState) -> dict[str, Any]:
        today = date.today()
        system = INTENT_SYSTEM.format(today=today.isoformat(), weekday=WEEKDAYS[today.weekday()])
        # 注入用户历史偏好作为默认值参考
        hint = profile_hint or state.profile_hint or ""
        if hint:
            system += f"\n\n用户历史偏好（仅供参考，以用户本次输入为准，用户未说的字段才用历史默认值）：\n{hint}"
        if state.effective_constraints:
            system += f"\n\n本次已确认的结构化约束（当前输入仍优先）：\n{_constraints_block(state)}"
        effective_query = state.query
        result: IntentExtraction = await ainvoke_structured(
            llm, [("system", system), ("human", effective_query)]
        )

        start = parse_iso_date(result.travel_start_date)
        end   = parse_iso_date(result.travel_end_date)
        destination = result.destination.strip() or None

        # travel_days 兜底：有出发日期 + 天数时，推算结束日期
        if start and not end and result.travel_days > 0:
            end = start + timedelta(days=result.travel_days - 1)

        missing: list[str] = []
        if not destination:
            missing.append("目的地")
        if not start:
            missing.append("出行开始日期")
        if not end:
            missing.append("出行结束日期")

        days = 0
        if start and end:
            if end < start:
                missing.append("结束日期早于开始日期")
            else:
                days = (end - start).days + 1

        # 天气预报（仅当目的地和日期均已确定时拉取）
        forecast: list[dict[str, Any]] = []
        w_note: str | None = None
        if not missing and destination and start and end:
            forecast, w_note = await fetch_weather_for_dates_async(
                destination, start, end, amap_key()
            )

        # 偏好归一化：去空白，并把 LLM 偶吐的 'null'/'无' 等占位垃圾值视为无偏好
        opt = clean_pref

        weather_log = f"，天气预报={len(forecast)}天" if forecast else ("，天气获取失败/超出范围" if not missing else "")
        note = (
            f"意图识别：destination={destination}，{start}~{end}（{days}天）"
            f"，景点偏好={opt(result.attraction_preference)}"
            f"，餐饮偏好={opt(result.food_preference)}"
            f"，习惯={opt(result.habit_preference)}"
            f"{weather_log}"
        )
        return {
            "destination": destination,
            "travel_start_date": start,
            "travel_end_date": end,
            "attraction_preference": opt(result.attraction_preference),
            "food_preference": opt(result.food_preference),
            "habit_preference": opt(result.habit_preference),
            "days": days,
            "missing_fields": missing,
            "weather_forecast": forecast,
            "weather_note": w_note,
            "history": state.history + [note],
        }

    return intent


def route_after_intent(state: TravelPlanState) -> str:
    return END if state.missing_fields else "query_rewrite"


async def weather_lookup_node(state: TravelPlanState, provider=None) -> dict[str, Any]:
    """Fetch weather from the confirmed brief, optionally from a frozen fixture provider."""
    if not (state.destination and state.travel_start_date and state.travel_end_date):
        raise ValueError("formal planning requires a confirmed destination and date range")
    if provider is None:
        forecast, weather_note = await fetch_weather_for_dates_async(
            state.destination,
            state.travel_start_date,
            state.travel_end_date,
            amap_key(),
        )
    else:
        supplied = provider(state.destination, state.travel_start_date, state.travel_end_date)
        if hasattr(supplied, "__await__"):
            supplied = await supplied
        if isinstance(supplied, tuple) and len(supplied) == 2:
            forecast, weather_note = supplied
        else:
            forecast, weather_note = supplied, "weather supplied by injected provider"
    return {
        "weather_forecast": forecast,
        "weather_note": weather_note,
        "history": state.history + [
            f"brief 已确认：{state.destination}，{state.travel_start_date}~{state.travel_end_date}（{state.days}天）；"
            f"天气预报={len(forecast)}天"
        ],
    }


# ─── 高德景点搜索 ─────────────────────────────────────────────

async def attraction_search_node(state: TravelPlanState, provider=None) -> dict[str, Any]:
    if provider is None:
        api_key = amap_key()
        spots = await fetch_city_spots_async(
            state.destination or "", api_key, max_spots=state.max_spots
        )
    else:
        spots = provider(state.destination or "", max_spots=state.max_spots)
        if hasattr(spots, "__await__"):
            spots = await spots
    from app.planning.poi_resolution import unique_named_poi
    from app.providers.amap import poi as poi_provider
    required_names = list(dict.fromkeys(
        match.group(1)
        for item in state.effective_constraints if item.get("polarity") == "require"
        for match in re.finditer(
            r"(?:必须去|一定要去|必去|第\s*\d+\s*天(?:去|安排))\s*([^，,；;。\s]+)",
            str(item.get("value_text") or ""),
        )
    ))
    existing_names = {item.get("name") for item in spots}
    missing = [name for name in required_names if name not in existing_names]
    semaphore = asyncio.Semaphore(4)

    async def supplement(name):
        async with semaphore:
            try:
                raw = await asyncio.wait_for(poi_provider.search_attraction_pois_async(
                    state.destination or "", amap_key(), keywords=name, types="",
                ), timeout=10)
                return unique_named_poi(raw, name)
            except Exception:
                return None

    # Keep provider work bounded even for a large or unresolved requirement set.
    supplements = await asyncio.gather(*(supplement(name) for name in missing[:16]))
    spots = [*spots, *(spot for spot in supplements if spot is not None)]
    kept, _ = filter_by_rating(spots, state.min_rating)
    # Food streets/night markets are meal destinations by default.  They only
    # become ordinary attraction candidates when the user explicitly makes
    # eating the trip focus; otherwise retain a single well-rated city feature.
    requirement_text = "\n".join(
        str(item.get("value_text") or "")
        for item in state.effective_constraints
        if item.get("polarity") == "require"
    )
    retained_names = {spot["name"] for spot in kept}
    food_focused = has_food_focus(state.effective_constraints, state.food_preference)
    for spot in spots:
        if (
            (infer_meal_scene(spot).meal_scene != "none" and (food_focused or is_featured_food_street(spot)))
            or (spot.get("name") and spot["name"] in requirement_text)
        ) and spot["name"] not in retained_names:
            kept.append(spot)
            retained_names.add(spot["name"])
    note = (f"高德景点搜索：抓取 {len(spots)} 个，评分/用餐场景/必去规则保留 {len(kept)} 个"
            f"；必去精确补搜 {min(len(missing), 16)} 项，命中 {sum(spot is not None for spot in supplements)} 项")
    return {"pois": kept, "history": state.history + [note]}


# ─── Candidate Builder + deterministic optimization ─────────

def make_candidate_builder_node(model_name: str | None, proposal_provider=None):
    # Candidate scoring is bounded classification over server-owned POIs.
    # An injected provider supplies frozen semantics in evaluation runs.
    llm = None
    if proposal_provider is None:
        llm = build_structured_llm(
            CandidatePoolProposal,
            provider="deepseek",
            model=(
                os.getenv("PLANNING_CANDIDATE_MODEL")
                or model_name
                or os.getenv("PLANNING_AGENT_MODEL")
                or None
            ),
            temperature=0,
        )

    async def candidate_builder(state: TravelPlanState) -> dict[str, Any]:
        cluster_map = cluster_pois_by_location(state.pois, max(1, state.days))
        candidate_text = format_spots_for_llm(state.pois, cluster_map)
        feedback = ""
        if state.candidate_repair_feedback:
            feedback = (
                "\n\n上次确定性求解/质量校验失败，仅修正候选语义，不要输出路线：\n"
                + json.dumps(state.candidate_repair_feedback, ensure_ascii=False)
            )
        prompt = (
            f"主 Agent 已确认的本次旅行需求（必须整体考虑，不能重新解释或忽略）：\n"
            f"{state.planning_instruction or state.query}\n\n"
            f"目的地：{state.destination}\n旅行天数：{state.days}\n"
            f"原行程中未被修改的内容作为连续性参考（不能凌驾于新确认约束）：\n{state.previous_plan_summary or '（无）'}\n\n"
            f"结构化约束（其中 require/avoid 必须遵守）：\n{_constraints_block(state)}\n\n"
            f"权威 POI 候选（只能逐字复制名称）：\n{candidate_text}"
            f"{feedback}"
        )
        proposals = []
        warnings = list(state.candidate_builder_warnings)
        try:
            if proposal_provider is None:
                result = await asyncio.wait_for(
                    ainvoke_structured(
                        llm, [("system", CANDIDATE_BUILDER_SYSTEM), ("human", prompt)]
                    ),
                    timeout=float(os.getenv("PLANNING_CANDIDATE_TIMEOUT_SECONDS", "45")),
                )
            else:
                result = proposal_provider(state)
                if hasattr(result, "__await__"):
                    result = await result
                if not isinstance(result, CandidatePoolProposal):
                    result = CandidatePoolProposal(candidates=result)
            proposals = result.candidates
        except Exception as exc:
            warnings.append(f"LLM_CANDIDATE_BUILDER_FAILED:{type(exc).__name__}")
        candidates, link_warnings = build_authoritative_candidates(
            proposals,
            state.pois,
            days=state.days,
            constraints=state.effective_constraints,
            attraction_preference=state.attraction_preference,
            food_focused=has_food_focus(state.effective_constraints, state.food_preference),
        )
        coverage = resolve_constraint_coverage(state.effective_constraints, state.constraint_coverage, state.pois, candidates, days=state.days)
        note = f"candidate_builder：冻结前候选 {len(candidates)} 个，语义警告 {len(link_warnings)} 条"
        return {
            "candidate_pool": [candidate.model_dump() for candidate in candidates],
            "candidate_pool_fingerprint": candidate_pool_fingerprint(candidates),
            "candidate_builder_warnings": warnings + link_warnings,
            "constraint_coverage": coverage,
            "quality_report": None,
            "history": state.history + [note],
        }

    return candidate_builder


async def optimize_attractions_node(state: TravelPlanState) -> dict[str, Any]:
    optimizer = AttractionSubsetOptimizer(state.scoring_profile_version)
    weather_by_day = {
        index + 1: weather
        for index, weather in enumerate(state.weather_forecast or [])
    }
    user_daily_min, user_daily_max = daily_bounds_from_constraints(state.effective_constraints)
    from app.core.transport import yuan_cents
    transfer_times = state.walking_time_minutes
    if state.transport_enabled:
        from app.planning.transport_check import transport_draft_minutes
        transfer_times = transport_draft_minutes(state)
    try:
        result = await asyncio.to_thread(
            optimizer.solve,
            state.candidate_pool,
            days=state.days,
            habit_preference=state.habit_preference,
            max_per_day=state.max_per_day,
            weather_by_day=weather_by_day,
            travel_start_date=state.travel_start_date,
            user_daily_min=user_daily_min,
            user_daily_max=user_daily_max,
            walking_time_minutes=transfer_times,
            transport_cost_cents=state.transport_cost_cents if state.transport_enabled else None,
            transport_budget_cents=yuan_cents(state.transport_budget) if state.transport_enabled else None,
        )
    except OptimizationFailure as exc:
        report = {
            "passed": False,
            "violations": [{"code": "NO_FEASIBLE_SOLUTION", "message": str(exc), "day": None, "poi_name": None}],
            "recomputed_objective": None,
            "solver_objective_delta": None,
            "daily_order_ratios": {},
        }
        verification = state.walking_time_verification
        if verification and verification.get("status") == "conflict" and state.walking_repair_round > 0:
            verification = {
                **verification,
                "status": "repair_no_feasible_solution",
                "verified": False,
                "re_solve_status": "NO_FEASIBLE_SOLUTION",
            }
        return {
            "route": state.walking_repair_original_route if verification and verification.get("status") == "repair_no_feasible_solution" else [],
            "quality_report": report,
            "solver_diagnostics": None,
            "walking_time_verification": verification,
            "history": state.history + [f"optimizer：{exc}"],
        }
    return {
        "route": result.route,
        "solver_diagnostics": result.diagnostics.model_dump(),
        "score_breakdown": result.score_breakdown,
        "quality_report": result.quality_report.model_dump(),
        "candidate_pool_frozen": True,
        "core_attractions_only": True,
        "approved": result.quality_report.passed,
        "history": state.history + [
            f"optimizer：{result.diagnostics.status}，objective={result.diagnostics.objective}，"
            f"quality={'PASS' if result.quality_report.passed else 'FAIL'}"
        ],
    }


def make_walking_time_node(leg_provider=None):
    async def walking_time_check(state: TravelPlanState) -> dict[str, Any]:
        if not state.route or not state.candidate_pool or not (state.quality_report or {}).get("passed"):
            return {}
        if leg_provider is None:
            from app.providers.amap.walking import query_walking_minutes
            provider = query_walking_minutes
        else:
            provider = leg_provider
        result = await verify_walking_schedule(
            state.route, state.candidate_pool, provider,
            travel_start_date=state.travel_start_date,
            max_corrections_per_day=0,
            walking_time_minutes=state.walking_time_minutes,
            max_new_queries=max(0, 60 - sum(len(targets) for targets in state.walking_time_minutes.values())),
            max_concurrent_requests=4,
        )
        verification = {
            "status": result.status,
            "verified": result.passed,
            "correction_attempts": result.correction_attempts,
            "violations": [item.__dict__ for item in result.violations],
        }
        report = dict(state.quality_report or {})
        report["walking_time_verification"] = verification
        if not result.passed:
            report["passed"] = False
            report["violations"] = list(report.get("violations") or []) + [
                {"code": item.code, "message": item.message, "day": item.day, "poi_name": item.poi_name}
                for item in result.violations
            ]
        repair_original = state.walking_repair_original_route
        repair_summary = list(state.walking_repair_summary)
        if result.status == "conflict" and not repair_original:
            repair_original = state.route
        elif result.status == "verified" and repair_original:
            def day_by_name(route):
                return {
                    str(spot.get("name")): int(day.get("day") or index)
                    for index, day in enumerate(route, 1)
                    for spot in day.get("spots") or []
                }
            before = day_by_name(repair_original)
            after = day_by_name(result.route)
            changes = []
            for name in sorted(before.keys() - after.keys()):
                changes.append(f"移除{name}（原第{before[name]}天）")
            for name in sorted(after.keys() - before.keys()):
                changes.append(f"加入{name}（第{after[name]}天）")
            for name in sorted(before.keys() & after.keys()):
                if before[name] != after[name]:
                    changes.append(f"{name}从第{before[name]}天调整到第{after[name]}天")
            repair_summary = changes
        return {
            "route": result.route,
            "quality_report": report,
            "walking_time_verification": verification,
            "walking_time_minutes": result.walking_time_minutes,
            "walking_repair_original_route": repair_original,
            "walking_repair_summary": repair_summary,
            "approved": result.passed,
            "history": state.history + [
                f"walking_time_check：{result.status}，修正尝试={result.correction_attempts}"
            ],
        }
    return walking_time_check

class PlanningQualityError(RuntimeError):
    pass


class WalkingTimePlanningError(PlanningQualityError):
    def __init__(self, verification: dict[str, Any], failure_snapshot: dict[str, Any] | None = None):
        self.verification = verification
        self.failure_snapshot = failure_snapshot or {}
        super().__init__(f"walking_time_{verification.get('status', 'unverified')}")


class UnsupportedPlanningConstraintError(PlanningQualityError):
    def __init__(self, coverage: list[dict[str, Any]]):
        self.coverage = coverage
        ids = ",".join(str(item.get("constraint_id") or "unknown") for item in coverage)
        super().__init__(f"unsupported_planning_constraints:{ids}")


class CandidateShortfallPlanningError(PlanningQualityError):
    def __init__(self, *, days: int, suggested_days: int, start_date: Any = None):
        self.days = days
        self.suggested_days = suggested_days
        self.start_date = start_date
        super().__init__("verified_candidate_pool_below_daily_minimum")


def _walking_failure_snapshot(state: TravelPlanState) -> dict[str, Any]:
    from app.planning.optimizer import daily_bounds_from_constraints

    daily_min, daily_max = daily_bounds_from_constraints(state.effective_constraints)
    return {
        "schema_version": 2,
        "days": state.days,
        "start_date": state.travel_start_date.isoformat() if state.travel_start_date else None,
        "end_date": state.travel_end_date.isoformat() if state.travel_end_date else None,
        "max_per_day": state.max_per_day,
        "max_spots": state.max_spots,
        "min_rating": state.min_rating,
        "daily_minimum": daily_min,
        "daily_maximum": daily_max,
        "trip_focus": state.trip_focus,
        "habit_preference": state.habit_preference,
        "transport_enabled": state.transport_enabled,
        "transport_budget": state.transport_budget,
        "allowed_transport_modes": state.allowed_transport_modes,
        "taxi_wait_min": state.taxi_wait_min,
        "taxi_boarding_min": state.taxi_boarding_min,
        "transport_cache": state.transport_cache,
        "transport_requests": state.transport_requests,
        "transport_time_minutes": state.transport_time_minutes,
        "transport_cost_cents": state.transport_cost_cents,
        "transport_failed_routes": state.transport_failed_routes,
        "transport_query_date": state.transport_query_date,
        "transport_verification": state.transport_verification,
        "scoring_profile_version": state.scoring_profile_version,
        "effective_constraints": [
            {
                key: constraint.get(key)
                for key in ("id", "fact_id", "category", "polarity", "value_text")
                if constraint.get(key) is not None
            }
            for constraint in state.effective_constraints[:64]
        ],
        "route": [
            {
                "day": int(day.get("day") or index),
                "spots": [
                    {
                        "name": str(spot.get("name") or ""),
                        "start_time": spot.get("start_time"),
                        "end_time": spot.get("end_time"),
                    }
                    for spot in day.get("spots") or []
                ],
            }
            for index, day in enumerate(state.route, 1)
        ],
        "candidate_pool": [
            item if isinstance(item, dict) else item.model_dump(mode="json")
            for item in state.candidate_pool[:60]
        ],
        "weather_forecast": state.weather_forecast[:14],
        "walking_time_minutes": state.walking_time_minutes,
        "verification": state.walking_time_verification,
        "quality_report": state.quality_report,
        "solver_diagnostics": state.solver_diagnostics,
        "repair_round": state.walking_repair_round,
    }


def quality_gate_node(state: TravelPlanState) -> dict[str, Any]:
    unresolved = [
        item for item in state.constraint_coverage
        if item.get("status") in {"unsupported", "unverified"}
        and next((constraint.get("polarity") for constraint in state.effective_constraints
                  if str(constraint.get("id") or constraint.get("fact_id") or "") == str(item.get("constraint_id") or "")), None)
        in {"require", "avoid"}
    ]
    if unresolved:
        raise UnsupportedPlanningConstraintError(unresolved)
    report = state.quality_report or {"passed": False, "violations": []}
    verification = state.walking_time_verification or {}
    if verification.get("status") in {"unverified", "budget_exhausted", "repair_no_feasible_solution"}:
        raise WalkingTimePlanningError(verification, _walking_failure_snapshot(state))
    if verification.get("status") == "conflict":
        if state.walking_repair_round >= state.max_walking_repair_rounds:
            raise WalkingTimePlanningError(
                {**verification, "status": "repair_limit_exhausted"},
                _walking_failure_snapshot(state),
            )
        return {
            "walking_repair_round": state.walking_repair_round + 1,
            "history": state.history + [
                f"quality_gate：真实步行冲突，开始第{state.walking_repair_round + 1}轮有界重求解"
            ],
        }
    no_feasible_route = any(
        item.get("code") == "NO_FEASIBLE_SOLUTION"
        for item in report.get("violations") or []
    )
    if no_feasible_route:
        daily_min, _daily_max = daily_bounds_from_constraints(state.effective_constraints)
        minimum = max(1, int(daily_min or 1))
        suggested_days = len(state.candidate_pool) // minimum
        coverage_by_id = {
            str(item.get("constraint_id") or ""): item.get("status")
            for item in state.constraint_coverage
        }
        required_attractions_applied = all(
            coverage_by_id.get(str(item.get("id") or item.get("fact_id") or "")) == "applied"
            for item in state.effective_constraints
            if item.get("category") == "attraction_preference" and item.get("polarity") == "require"
        )
        mandatory_count = sum(
            bool(candidate.get("must_visit") if isinstance(candidate, dict) else candidate.must_visit)
            for candidate in state.candidate_pool
        )
        if (
            suggested_days > 0
            and suggested_days < state.days
            and required_attractions_applied
            and mandatory_count <= suggested_days * max(1, state.max_per_day)
            and not any(
                int((candidate.get("fixed_day") if isinstance(candidate, dict) else getattr(candidate, "fixed_day", 0)) or 0) > suggested_days
                for candidate in state.candidate_pool
            )
        ):
            raise CandidateShortfallPlanningError(
                days=state.days, suggested_days=suggested_days,
                start_date=state.travel_start_date,
            )
    if report.get("passed"):
        return {"approved": True}
    if state.candidate_repair_round < state.max_candidate_repair_rounds:
        return {
            "candidate_repair_round": state.candidate_repair_round + 1,
            "candidate_repair_feedback": list(report.get("violations") or []),
            "candidate_pool_frozen": False,
            "history": state.history + ["quality_gate：失败，触发唯一一次 Candidate Repair"],
        }
    if verification.get("status") in {"conflict", "unverified", "budget_exhausted"}:
        raise WalkingTimePlanningError(verification, _walking_failure_snapshot(state))
    codes = ",".join(str(item.get("code") or "UNKNOWN") for item in report.get("violations") or [])
    raise PlanningQualityError(f"itinerary rejected after candidate repair: {codes}")


def route_after_quality_gate(state: TravelPlanState) -> str:
    verification = state.walking_time_verification or {}
    if verification.get("status") == "conflict":
        return "optimizer"
    return "finalize" if (state.quality_report or {}).get("passed") else "candidate_builder"


# ─── Planner ─────────────────────────────────────────────────

def _travel_dates_block(state: TravelPlanState) -> str:
    """逐天『日期（星期）』块，供 planner/reviewer 判断景点当天是否开放
    （闭馆日/限定开放日，如『周一闭馆』『周三至周日开放』）。无出发日期时返回空串。"""
    if not state.travel_start_date or not state.days:
        return ""
    lines = [
        f"  Day{i + 1} = {(state.travel_start_date + timedelta(days=i)).isoformat()}"
        f"（{WEEKDAYS[(state.travel_start_date + timedelta(days=i)).weekday()]}）"
        for i in range(state.days)
    ]
    return (
        "\n\n出行日期与星期（请据此判断景点当天是否开放，"
        "勿把有闭馆日/限定开放日的景点排在其不开放的星期）：\n" + "\n".join(lines)
    )


def make_planner_node(model_name: str | None):
    llm = _build_planning_llm(TravelRoute, model_name, temperature=0.3)

    async def planner(state: TravelPlanState) -> dict[str, Any]:
        # ① 上一轮景点集合（用于 spot diff，检测"notes 说改但 JSON 未变"）
        old_spots = {s["name"] for day in (state.route or []) for s in day.get("spots", [])}
        # ② 是否最终修订轮（review_round 尚未 +1 时检测）
        is_final = (state.review_round >= state.max_review_rounds
                    and bool(state.route_modify_opinion))

        cluster_map = cluster_pois_by_location(state.pois, state.days)
        cand_text = format_spots_for_llm(state.pois, cluster_map)
        feedback = ""
        if state.route_modify_opinion:
            is_user_opinion = "【用户修改意见】" in state.route_modify_opinion
            opinion_label = (
                "用户直接修改意见（最高优先级，必须全部响应）"
                if is_user_opinion else
                "评审修改意见（请据此修订）"
            )
            # 上一轮检测到未改动时注入高优先级警告
            stale_block = ""
            if state.route_stale_warning:
                stale_block = (
                    f"\n\n🚨🚨【严重警告：上一轮你的输出与上上轮完全相同，days 字段一个景点都没变！】\n"
                    f"具体记录：{state.route_stale_warning}\n"
                    f"这说明你只改了 reasoning/notes 文字，但 days 里的景点列表原封不动地回显了旧版本。\n"
                    f"本轮你必须真正修改 days——至少换掉评审意见中明确要求替换的景点，"
                    f"或重新分配各天的景点组合。如果 days 再次与上一版完全相同，将被系统标记为规划失败。\n"
                )
            feedback = (
                f"\n\n上一版路线：\n{json.dumps(state.route, ensure_ascii=False)}\n\n"
                f"{stale_block}"
                f"{opinion_label}：\n{state.route_modify_opinion}"
            )

        weather_text = format_weather_for_llm(state.weather_forecast)
        weather_block = (
            f"\n\n出行天气预报：\n{weather_text}"
            if weather_text else "\n\n（无天气信息，按晴天规划）"
        )

        # 历轮沟通记录：让 Planner 知道自己已响应了哪些意见
        dialogue_block = ""
        if state.planner_reviewer_dialogue:
            dialogue_text = "\n".join(state.planner_reviewer_dialogue)
            dialogue_block = (
                f"\n\n【历轮沟通记录（请对照，确认哪些意见已响应、哪些【紧急必须优先改】仍未修复）】\n"
                f"{dialogue_text}"
            )

        # ③ 最终修订提示（末轮告知 planner 这是最后机会）
        final_note = (
            "\n\n【最终修订】此轮是最后一次修订机会，之后不再评审。"
            "你要站在产品的角度，给出不会影响产品体验，不会让用户流失的路线，无需逐条解释修改原因。"
            if is_final else ""
        )

        prompt = (
            f"用户需求：{state.rewritten_query or state.query}\n"
            f"目的地：{state.destination}\n旅行天数：{state.days} 天\n"
            f"每天景点数上限：{state.max_per_day}\n"
            f"景点偏好：{state.attraction_preference or '无'}\n"
            f"游玩习惯/节奏：{state.habit_preference or '无'}\n"
            f"本次结构化约束：\n{_constraints_block(state, {'attraction_preference', 'travel_pace', 'schedule_preference', 'companion_context', 'transport_preference', 'accessibility_need', 'budget_style', 'other_travel_preference'})}"
            f"{_travel_dates_block(state)}"
            f"{weather_block}\n\n"
            f"候选景点池（共 {len(state.pois)} 个）：\n{cand_text}"
            f"{feedback}"
            f"{dialogue_block}"
            f"{final_note}\n\n"
            f"请给出 {state.days} 天带时刻表的逐天景点安排。"
        )
        result: TravelRoute = await ainvoke_structured(llm, [("system", PLANNER_SYSTEM), ("human", prompt)])
        route = [d.model_dump() for d in result.days]
        rnd = state.review_round + 1

        logger.info("[planner 第%d轮] reasoning：\n%s", rnd, result.reasoning or "(空)")

        # ④ spot diff：检测实际是否有景点变化，透明化"说改但没改"
        new_spots = {s["name"] for day in route for s in day.get("spots", [])}
        added   = new_spots - old_spots
        removed = old_spots - new_spots
        change_summary = ""
        if old_spots and (added or removed):
            parts = []
            if added:   parts.append(f"新增：{'、'.join(sorted(added))}")
            if removed: parts.append(f"移除：{'、'.join(sorted(removed))}")
            change_summary = f"（{' | '.join(parts)}）"

        note         = f"[第{rnd}轮] Planner 出稿：{result.notes or '(无说明)'}"
        planner_line = f"[第{rnd}轮] Planner：{result.notes or '(无说明)'}{change_summary}"

        # ⑤ route 与上一轮完全相同时写 warning + 设置 stale_warning 供下一轮注入
        history = state.history
        old_route_json = json.dumps(state.route, ensure_ascii=False, sort_keys=True)
        new_route_json = json.dumps(route, ensure_ascii=False, sort_keys=True)
        is_unchanged = bool(state.route_modify_opinion and state.route and old_route_json == new_route_json)
        new_stale_warning = (
            f"第{rnd}轮 Planner 输出的 route JSON 与第{rnd - 1}轮完全一致，days 字段零改动。"
            if is_unchanged else ""
        )
        if is_unchanged:
            history = history + [
                f"⚠️ [第{rnd}轮] Planner route 与上一轮完全相同，未作任何修改",
            ]
        history = history + [note]

        return {
            "route": route,
            "review_round": rnd,
            "history": history,
            "planner_reviewer_dialogue": state.planner_reviewer_dialogue + [planner_line],
            "modification_concern": result.modification_concern or None,
            "route_stale_warning": new_stale_warning,
        }

    return planner


# ─── Reviewer ────────────────────────────────────────────────

def make_reviewer_node(model_name: str | None):
    llm = _build_planning_llm(RouteReview, model_name, temperature=0)

    async def reviewer(state: TravelPlanState) -> dict[str, Any]:
        bad_unknown = unknown_spots(state.route, state.pois)

        facts = f"非候选池景点：{('；'.join(bad_unknown)) or '无'}"

        weather_text = format_weather_for_llm(state.weather_forecast)
        weather_block = (
            f"\n出行天气预报：\n{weather_text}\n"
            if weather_text else "\n（无天气信息）\n"
        )

        # 历轮沟通记录：让 Reviewer 知道自己之前提过哪些非时间类问题、是否已被修复
        dialogue_block = ""
        if state.planner_reviewer_dialogue:
            # 过滤掉 time_check 相关的对话，避免 reviewer 看到时间冲突信息
            non_time_lines = [
                line for line in state.planner_reviewer_dialogue
                if "[time_check" not in line
            ]
            if non_time_lines:
                dialogue_text = "\n".join(non_time_lines)
                dialogue_block = (
                    f"\n\n【历轮沟通记录（检查你之前标注的问题是否已被修复；"
                    f"未修复则继续指出，已修复则审查新问题）】\n{dialogue_text}"
                )

        prompt = (
            f"目的地：{state.destination}，共 {state.days} 天，每天上限 {state.max_per_day}。\n"
            f"用户游玩习惯：{state.habit_preference or '无'}\n"
            f"本次路线约束：\n{_constraints_block(state, {'travel_pace', 'schedule_preference', 'companion_context', 'transport_preference', 'accessibility_need', 'attraction_preference'})}"
            f"{_travel_dates_block(state)}\n"
            f"{weather_block}\n"
            f"候选景点池：\n{format_spots_for_llm(state.pois, cluster_pois_by_location(state.pois, state.days))}\n\n"
            f"待评审路线：\n{json.dumps(state.route, ensure_ascii=False)}\n\n"
            f"系统客观预检（请据此判断）：\n{facts}"
            f"{dialogue_block}\n\n"
            f"请评审并给出结论。⚠️ 开放时间和闭馆日由 time_check 专项 Agent 单独核查，"
            f"你不要评审开放时间相关问题。"
        )
        result: RouteReview = await ainvoke_structured(llm, [("system", REVIEWER_SYSTEM), ("human", prompt)])
        approved = result.approved and not bad_unknown
        verdict  = "✅通过" if approved else "❌打回"

        # ── 后端日志：推理过程 + 审查结论（不进 history / planner_reviewer_dialogue）──
        logger.debug(
            "[Reviewer 第%d轮] 推理过程：\n%s",
            state.review_round, result.reasoning,
        )
        logger.info(
            "[Reviewer 第%d轮] %s（%d分）opinion=%r  issues=%r",
            state.review_round, verdict, result.score,
            result.route_modify_opinion or "(无)", result.issues,
        )

        # 完整写入 reviewer 意见，不截断，让用户在规划日志里看到完整评审过程
        opinion_full = result.route_modify_opinion or "(无意见)"
        issues_full  = ("；".join(result.issues)) if result.issues else ""
        note = (
            f"[第{state.review_round}轮] Reviewer {verdict}（{result.score}分）：{opinion_full}"
            + (f"\n  → 问题列表：{issues_full}" if issues_full else "")
        )

        # 追加本轮 Reviewer 记录到共享对话
        reviewer_line = (
            f"[第{state.review_round}轮] Reviewer {'通过' if approved else '打回'}"
            f"（{result.score}分）：{result.route_modify_opinion or '(无意见)'}"
        )
        return {
            "approved": approved,
            "need_modify_route": not approved,
            "route_modify_opinion": result.route_modify_opinion,
            "reviewer_issues": result.issues,
            "history": state.history + [note],
            "planner_reviewer_dialogue": state.planner_reviewer_dialogue + [reviewer_line],
        }

    return reviewer


def route_after_review(state: TravelPlanState) -> str:
    """主循环结束（通过 或 达最大轮数）→ time_check；否则还给 planner。

    终止循环的判断从 route_after_planner 移到这里，确保每一份最终路线都经过 reviewer 评估。
    然后进入 time_check 阶段独立核查开放时间，reviewer 不再处理时间问题。
    """
    if state.approved or state.review_round > state.max_review_rounds:
        return "time_check"
    return "planner"


def route_after_planner(state: TravelPlanState) -> str:
    """planner 输出的下一跳：

    - 已进入 time_check 阶段（time_check_done=True）：回 time_check 重新核查时间
    - 否则：进 reviewer 走主循环

    设计：time_check_done 是单向门——一旦置 True，planner 永远不再回 reviewer。
    """
    if state.time_check_done:
        return "time_check"
    return "reviewer"


def route_after_time_check(state: TravelPlanState) -> str:
    """time_check 输出的下一跳：

    - 无违规 → meal_search（时间合法）
    - 达 max_time_check_rounds 上限 → meal_search（带剩余问题前进，由 finalize 透传给前端）
    - 否则 → planner 修正
    """
    if not state.time_violations:
        return "meal_search"
    if state.time_check_round >= state.max_time_check_rounds:
        return "meal_search"
    return "planner"


# ─── time_check 专项 Agent ──────────────────────────────────

def make_time_check_node(model_name: str | None):
    """开放时间核查专家 Agent：CoT 推理 + 仅输出违规。

    职责单一——只判断每个景点的 start_time/end_time 是否符合开放时间和闭馆日；
    其他维度（地理、习惯、天气、合法性）一概不管。
    """
    llm = _build_planning_llm(TimeCheckResult, model_name, temperature=0)

    async def time_check(state: TravelPlanState) -> dict[str, Any]:
        rnd = state.time_check_round + 1

        # 拼当天日期/星期 + 每个景点的 安排时段 + 开放原文
        open_map = {s["name"]: (s.get("open_time") or "未知") for s in state.pois}
        lines: list[str] = []
        for day in state.route:
            day_no = day.get("day")
            for spot in day.get("spots", []):
                lines.append(
                    f"  Day{day_no} {spot.get('name')} 安排 "
                    f"{spot.get('start_time')}-{spot.get('end_time')} | "
                    f"开放原文：{open_map.get(spot.get('name'), '未知')}"
                )
        route_block = "\n".join(lines) if lines else "  （路线为空）"

        prompt = (
            f"目的地：{state.destination}"
            f"{_travel_dates_block(state)}\n\n"
            f"待核查的景点安排（每行格式：Day N 景点名 安排 start-end | 开放原文：...）：\n"
            f"{route_block}\n\n"
            f"请按 schema 字段顺序输出：先 reasoning 逐景点推理，再 violations 仅写确认违规的项。"
        )

        try:
            result: TimeCheckResult = await ainvoke_structured(
                llm, [("system", TIME_CHECK_SYSTEM), ("human", prompt)], retries=3
            )
        except RuntimeError:
            # 静默降级：不阻塞主流程，让用户能拿到行程
            logger.warning("[time_check 第%d轮] LLM 调用失败，跳过时间核查", rnd)
            return {
                "time_violations": [],
                "time_check_done": True,
                "time_check_round": rnd,
                "history": state.history + [f"[time_check 第{rnd}轮] LLM 调用失败，跳过时间核查"],
            }

        # ── 后端日志：推理过程 + 审查结论 ──────────────────────────
        logger.debug(
            "[time_check 第%d轮] 推理过程：\n%s",
            rnd, result.reasoning,
        )
        if result.violations:
            violation_lines = "\n".join(
                f"  Day{v.day} {v.spot_name}：{v.detail}" for v in result.violations
            )
            logger.info(
                "[time_check 第%d轮] ❌ 发现 %d 处违规：\n%s",
                rnd, len(result.violations), violation_lines,
            )
        else:
            logger.info("[time_check 第%d轮] ✅ 无违规，时间安排全部合法", rnd)

        violations_dicts = [v.model_dump() for v in result.violations]
        if not violations_dicts:
            return {
                "time_violations": [],
                "time_check_done": True,
                "time_check_round": rnd,
                "history": state.history + [f"[time_check 第{rnd}轮] ✅ 无违规"],
                "planner_reviewer_dialogue": state.planner_reviewer_dialogue
                    + [f"[time_check 第{rnd}轮] 无违规"],
            }

        # 有违规：组装定向修正指令给 planner
        detail_lines = "\n".join(
            f"- Day{v.day} {v.spot_name}：{v.detail}" for v in result.violations
        )
        opinion = (
            f"【开放时间修正（第{rnd}轮）】仅修正以下时段冲突，其余安排保持不变：\n{detail_lines}"
        )
        note = f"[time_check 第{rnd}轮] 发现 {len(violations_dicts)} 处冲突：\n{detail_lines}"

        return {
            "time_violations": violations_dicts,
            "time_check_done": True,
            "time_check_round": rnd,
            "route_modify_opinion": opinion,
            "approved": False,  # 有时间问题就视为未通过
            "history": state.history + [note],
            "planner_reviewer_dialogue": state.planner_reviewer_dialogue + [note],
        }

    return time_check


# ─── 餐饮搜索 ────────────────────────────────────────────────

async def meal_search_node(state: TravelPlanState) -> dict[str, Any]:
    api_key  = amap_key()
    loc_map  = spot_location_map(state.pois)
    meal_candidates: list[dict[str, Any]] = []
    warnings: list[str] = []

    for day in state.route:
        day_no = day.get("day")
        entry: dict[str, Any] = {"day": day_no, "lunch": {}, "dinner": {}}

        lunch_anchor  = last_spot_of_period(day, "morning")
        dinner_anchor = dinner_anchor_spot(day)

        for meal, anchor in (("lunch", lunch_anchor), ("dinner", dinner_anchor)):
            center = loc_map.get(anchor["name"]) if anchor else None
            if not center:
                warnings.append(f"Day{day_no} {meal} 无中心景点坐标")
                entry[meal] = {"anchor": anchor["name"] if anchor else None, "candidates": []}
                continue
            raw = await search_around_pois_async(
                center,
                api_key,
                types="餐饮服务",
                radius=1000,
                offset=20,
            )
            cands = [r for r in (restaurant_to_dict(p) for p in raw) if r][:20]
            if not cands:
                warnings.append(f"Day{day_no} {meal}（{anchor['name']} 周边）无餐饮")
            entry[meal] = {"anchor": anchor["name"], "center": center, "candidates": cands}
        meal_candidates.append(entry)

    note = "周边餐饮搜索完成" + (f"（提醒：{'; '.join(warnings)}）" if warnings else "")
    return {"meal_candidates": meal_candidates, "history": state.history + [note]}


# ─── 餐厅推荐 ────────────────────────────────────────────────

def make_meal_recommend_node(model_name: str | None):
    llm = _build_planning_llm(SingleDayMealPick, model_name, temperature=0)

    async def meal_recommend(state: TravelPlanState) -> dict[str, Any]:

        def _top(cands: list[dict[str, Any]], n: int = 10) -> list[dict[str, Any]]:
            """按评分降序取前 n 家，减少喂给 LLM 的 token。"""
            return sorted(cands, key=lambda c: -(c.get("rating") or 0))[:n]

        def _fmt(cands: list[dict[str, Any]]) -> str:
            if not cands:
                return "（无候选）"
            return "\n".join(
                f"  · {c['name']}（评分 {c['rating'] or '无'}，人均 {c['cost'] or '无'}，"
                f"标签 {c['keytag'] or '无'}）"
                for c in cands
            )

        async def _recommend_day(entry: dict[str, Any]) -> DayMealPick:
            """单天 LLM 调用；失败时取评分最高的餐厅降级兜底。"""
            lunch_cands  = _top(entry["lunch"].get("candidates", []))
            dinner_cands = _top(entry["dinner"].get("candidates", []))
            prompt = (
                f"第 {entry['day']} 天 | 用户用餐偏好：{state.food_preference or '无特别偏好'}\n\n"
                f"本次餐饮与预算约束：\n{_constraints_block(state, {'food_preference', 'dietary_requirement', 'budget_style'})}\n\n"
                f"午餐候选（{entry['lunch'].get('anchor')} 周边）：\n{_fmt(lunch_cands)}\n\n"
                f"晚餐候选（{entry['dinner'].get('anchor')} 周边）：\n{_fmt(dinner_cands)}\n\n"
                "请选出今天的午餐和晚餐。"
            )
            try:
                r: SingleDayMealPick = await ainvoke_structured(
                    llm, [("system", MEAL_SYSTEM), ("human", prompt)], retries=5
                )
                return DayMealPick(day=entry["day"], **r.model_dump())
            except RuntimeError:
                def best(cands: list) -> str:
                    return cands[0]["name"] if cands else ""
                return DayMealPick(
                    day=entry["day"],
                    lunch_name=best(lunch_cands),
                    lunch_reason="（系统自动选取评分最高餐厅）",
                    dinner_name=best(dinner_cands),
                    dinner_reason="（系统自动选取评分最高餐厅）",
                )

        # 不同天并行调用 LLM；gather 保持与输入相同的确定性顺序。
        day_picks: list[DayMealPick] = list(
            await asyncio.gather(
                *(_recommend_day(entry) for entry in state.meal_candidates)
            )
        )

        def _lookup(name: str, cands_dict: dict[str, Any], cands_list: list[dict]) -> dict | None:
            """子串匹配候选餐厅；LLM 名称改写时宽松匹配（含子串即算）。

            name 为空字符串代表"LLM 认为无偏好匹配而主动放弃"，不等于候选列表为空。
            ⚠️ 不能在 name 为空时提前 return None——那会让有 20 家候选的餐次也显示"暂无"。
            正确语义：只要 cands_list 非空就必有返回，None 只意味着候选列表确实为空。
            """
            if name:
                for key, val in cands_dict.items():
                    if name in key or key in name:
                        return val
            return cands_list[0] if cands_list else None

        pick_map = {p.day: p for p in day_picks}
        meals: list[dict[str, Any]] = []
        for entry in state.meal_candidates:
            pick              = pick_map.get(entry["day"])
            lunch_cands_list  = sorted(entry["lunch"].get("candidates", []),  key=lambda c: -(c.get("rating") or 0))
            dinner_cands_list = sorted(entry["dinner"].get("candidates", []), key=lambda c: -(c.get("rating") or 0))
            lunch_cands       = {c["name"]: c for c in lunch_cands_list}
            dinner_cands      = {c["name"]: c for c in dinner_cands_list}
            lunch_info   = _lookup(pick.lunch_name,  lunch_cands,  lunch_cands_list)  if pick else None
            dinner_info  = _lookup(pick.dinner_name, dinner_cands, dinner_cands_list) if pick else None

            if lunch_info is not None:
                lunch_info  = {**lunch_info,  "reason": pick.lunch_reason}
            if dinner_info is not None:
                dinner_info = {**dinner_info, "reason": pick.dinner_reason}

            # 确定性兜底：午/晚餐重复时换评分次高的一家
            if (lunch_info is not None and dinner_info is not None
                    and pick and pick.lunch_name == pick.dinner_name):
                alt = next(
                    (c for c in sorted(
                        entry["dinner"].get("candidates", []),
                        key=lambda c: -(c.get("rating") or 0),
                    ) if c["name"] != pick.lunch_name),
                    None,
                )
                if alt:
                    dinner_info = {**alt, "reason": f"（系统调整：避免与午餐重复，改选 {alt['name']}）"}
                else:
                    dinner_info = {**dinner_info,
                                   "reason": (dinner_info.get("reason") or "")
                                   + " ⚠️ 该区域仅此一家餐厅，午晚餐相同，出行前请确认周边餐饮。"}

            meals.append({"day": entry["day"], "lunch": lunch_info, "dinner": dinner_info})

        note = "餐厅推荐完成：" + "，".join(
            f"Day{m['day']} 午={m['lunch']['name'] if m['lunch'] else '无'}"
            f"/晚={m['dinner']['name'] if m['dinner'] else '无'}"
            for m in meals
        )
        return {"meals": meals, "history": state.history + [note]}

    return meal_recommend


# ─── finalize ────────────────────────────────────────────────

# ─── 景点游玩贴士 ─────────────────────────────────────────────

def make_spot_tips_node(model_name: str | None):
    """为行程中每个景点生成游玩注意事项（结合当天天气 + 景点属性 + 独有常识）。

    非关键路径：LLM 失败时降级为无贴士，不阻塞行程生成。
    """
    llm = _build_spot_tips_llm(model_name)

    async def spot_tips_node(state: TravelPlanState) -> dict[str, Any]:
        spot_names: list[str] = []
        lines: list[str] = []
        for day in state.route:
            day_no = day.get("day")
            the_date = ""
            if state.travel_start_date and day_no:
                d = state.travel_start_date + timedelta(days=day_no - 1)
                the_date = f"{d.isoformat()} {WEEKDAYS[d.weekday()]}"
            lines.append(f"第 {day_no} 天（{the_date or '日期未知'}）：")
            for spot in day.get("spots", []):
                spot_names.append(spot["name"])
                lines.append(
                    f"  · {spot['name']}（{spot.get('period')} {spot.get('start_time')}–{spot.get('end_time')}）"
                )
        if not spot_names:
            return {}

        weather_text = format_weather_for_llm(state.weather_forecast) or "（无可用天气预报）"
        prompt = (
            f"目的地：{state.destination}\n\n"
            f"同行、节奏与无障碍提示约束：\n{_constraints_block(state, {'travel_pace', 'schedule_preference', 'companion_context', 'accessibility_need', 'transport_preference'})}\n\n"
            "行程：\n" + "\n".join(lines) + "\n\n"
            f"逐天天气预报：\n{weather_text}"
        )
        try:
            result: SpotTipsResult = await asyncio.wait_for(
                ainvoke_structured(
                    llm, [("system", SPOT_TIPS_SYSTEM), ("human", prompt)]
                ),
                timeout=float(os.getenv("PLANNING_SPOT_TIPS_TIMEOUT_SECONDS", "30")),
            )
        except Exception as exc:
            # Tips are enrichment only: provider failures and timeouts must
            # never hold the validated itinerary hostage.
            return {
                "history": state.history + [
                    f"spot_tips：贴士生成失败，已跳过（{type(exc).__name__}）"
                ]
            }

        # 名称匹配：先精确，再子串宽松兜底（LLM 偶发轻微改写名称）
        valid = set(spot_names)
        tips = {t.name: t.tip.strip() for t in result.tips if t.name in valid and t.tip.strip()}
        for t in result.tips:
            if t.name not in valid and t.tip.strip():
                for name in valid:
                    if name not in tips and (t.name in name or name in t.name):
                        tips[name] = t.tip.strip()
                        break

        note = f"spot_tips：为 {len(tips)}/{len(valid)} 个景点生成游玩贴士"
        return {"spot_tips": tips, "history": state.history + [note]}

    return spot_tips_node


def make_finalize_node(memory_writer=None):
    def finalize_node(state: TravelPlanState) -> dict[str, Any]:
        result = _finalize_impl(state)
        if memory_writer and result.get("final_plan"):
            try:
                memory_writer(result["final_plan"], state)
            except Exception:
                pass  # 记忆写入失败不影响主流程
        return result
    return finalize_node


def finalize_node(state: TravelPlanState) -> dict[str, Any]:
    return _finalize_impl(state)


def _finalize_impl(state: TravelPlanState) -> dict[str, Any]:
    """Assemble the immutable core itinerary.

    New optimizer runs contain attraction items only.  Legacy revision runs
    retain their historical meal projection because Revision Graph migration is
    intentionally outside this change.
    """
    spot_info    = {s["name"]: s for s in state.pois}
    meals_by_day = {m["day"]: m for m in state.meals}

    days_out: list[dict[str, Any]] = []
    for day in state.route:
        day_no   = day.get("day")
        the_date = None
        if state.travel_start_date:
            the_date = (state.travel_start_date + timedelta(days=day_no - 1)).isoformat()

        meal     = meals_by_day.get(day_no, {})
        timeline: list[dict[str, Any]] = []

        # 用名称相等（非 `is`）避免 LangGraph 序列化/反序列化后身份比较失效
        morning_anchor_name   = (last_spot_of_period(day, "morning") or {}).get("name")
        afternoon_anchor_name = (last_spot_of_period(day, "afternoon") or {}).get("name")
        lunch_inserted  = False
        dinner_inserted = False

        for spot in day.get("spots", []):
            info = spot_info.get(spot["name"], {})
            timeline.append({
                "type": "attraction",
                "name": spot["name"],
                "start_time": spot.get("start_time"),
                "end_time": spot.get("end_time"),
                "period": spot.get("period"),
                "rating": info.get("rating"),
                "open_time": info.get("open_time"),
                "photo": info.get("photo"),
                "photos": info.get("photos") or [],
                "poi_id": info.get("id") or info.get("poi_id"),
                "location": info.get("location"),
                "tip": state.spot_tips.get(spot["name"]),
                "address": info.get("address"),
                "tel": info.get("tel"),
                "cost": info.get("cost"),
                "meal_scene": spot.get("meal_scene", info.get("meal_scene", "none")),
                "meal_coverage": spot.get("meal_coverage"),
                "walking_from_previous_min": spot.get("walking_from_previous_min"),
                "walking_time_source": spot.get("walking_time_source"),
                "transport_from_previous": spot.get("transport_from_previous"),
                "semantic_source": spot.get("semantic_source", info.get("semantic_source")),
            })
            if not state.core_attractions_only and spot.get("name") == morning_anchor_name and not lunch_inserted:
                lunch_inserted = True
                if meal.get("lunch"):
                    timeline.append({"type": "lunch", **meal["lunch"]})
                else:
                    timeline.append({"type": "lunch", "name": None, "no_restaurant": True})
            if not state.core_attractions_only and spot.get("name") == afternoon_anchor_name and not dinner_inserted:
                dinner_inserted = True
                if meal.get("dinner"):
                    timeline.append({"type": "dinner", **meal["dinner"]})
                else:
                    timeline.append({"type": "dinner", "name": None, "no_restaurant": True})

        # 相邻地点 haversine 距离
        for i in range(1, len(timeline)):
            prev_loc = timeline[i - 1].get("location")
            cur_loc  = timeline[i].get("location")
            if prev_loc and cur_loc:
                timeline[i]["dist_from_prev_km"] = round(haversine_km(prev_loc, cur_loc), 2)

        days_out.append({"day": day_no, "date": the_date, "theme": day.get("theme"), "timeline": timeline})

    final_plan = {
        "query": state.query,
        "planning_instruction": state.planning_instruction,
        "destination": state.destination,
        "start_date": state.travel_start_date.isoformat() if state.travel_start_date else None,
        "end_date": state.travel_end_date.isoformat() if state.travel_end_date else None,
        "days_count": state.days,
        "preferences": {
            "attraction": state.attraction_preference,
            "food": state.food_preference,
            "habit": state.habit_preference,
        },
        "trip_budget": state.trip_budget,
        "transport_budget": state.transport_budget,
        "transport_summary": state.transport_summary,
        "transport_verification": state.transport_verification,
        "effective_constraints": state.effective_constraints,
        "constraint_coverage": state.constraint_coverage,
        "planning_notes": [
            "住宿偏好已作为出行建议保留；当前版本未接入酒店搜索或预订数据。"
            for item in state.effective_constraints
            if item.get("category") == "accommodation_preference"
        ][:1],
        "approved": state.approved,
        "review_rounds": state.review_round,
        "weather_forecast": state.weather_forecast,
        "weather_note": state.weather_note,
        # 透传给前端的"出行注意事项"：来自 reviewer 最后一轮的 issues，
        # 已是给用户看的友好出行提醒。time_check 的 violations 不属于注意事项——
        # 它要么被 planner 修完（time_violations 清空），要么属于极端兜底情况（达轮数上限未清完），
        # 不是给用户的常规提醒。
        "route_issues": list(state.reviewer_issues or []),
        "scoring_profile": state.scoring_profile_version if state.core_attractions_only else None,
        "candidate_semantics_version": state.candidate_semantics_version if state.core_attractions_only else None,
        "candidate_pool_fingerprint": state.candidate_pool_fingerprint,
        "solver_diagnostics": state.solver_diagnostics,
        "score_breakdown": state.score_breakdown,
        "quality_report": state.quality_report,
        "walking_time_verification": state.walking_time_verification,
        "days": days_out,
    }
    placed_names = {s["name"] for day_r in state.route for s in day_r.get("spots", [])}
    candidate_spots = [
        {k: v for k, v in s.items()
         if k in ("id", "poi_id", "name", "rating", "photo", "photos", "location", "open_time", "address", "type", "typecode")}
        for s in state.pois
        if s.get("name") and s["name"] not in placed_names
    ][:20]
    final_plan["candidate_spots"] = candidate_spots

    history = state.history + ["finalize：已组装最终计划"]
    # 规划过程日志随 plan 一起落库，历史详情页回看时可还原完整规划过程
    final_plan["history"] = history
    return {"final_plan": final_plan, "history": history}

"""Deterministic attraction subset, routing, scheduling, and quality checks."""

from __future__ import annotations

import itertools
import math
import re
import time
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Any, Iterable, Mapping, Sequence

try:
    from ortools.sat.python import cp_model
except ImportError:  # pragma: no cover - exercised only before dependencies install
    cp_model = None  # type: ignore[assignment]

from app.core.daily_counts import daily_bound_for_constraint, daily_bounds_from_constraints
from app.planning.helpers import haversine_km
from app.planning.scoring import ScoringProfile, evaluate_itinerary, load_scoring_profile
from app.planning.schemas import (
    OptimizerCandidate,
    QualityReport,
    QualityViolation,
    SolverDiagnostics,
)
from app.planning.walking_identity import walking_minutes_for, walking_place_key

DAY_START = 9 * 60
DAY_END = 21 * 60
TRANSFER_MIN = 20
LUNCH_WINDOW = (11 * 60 + 30, 13 * 60 + 30)
DINNER_WINDOW = (17 * 60 + 30, 20 * 60)
MIN_MEAL_OVERLAP = 45
DEFAULT_SOLVER_SECONDS = 1.0
DEFAULT_RANDOM_SEED = 20260816


def transfer_minutes(cache, origin, destination):
    """Supplier time when known; a distance estimate only for an unverified draft."""
    if origin is None or destination is None:
        return 0
    def location(place):
        return place.location if isinstance(place, OptimizerCandidate) else place["location"]
    estimate = max(1, math.ceil(haversine_km(location(origin), location(destination)) * 12))
    return walking_minutes_for(cache, origin, destination, estimate)


class OptimizationFailure(RuntimeError):
    """Raised when CP-SAT and deterministic fallback cannot produce a route."""


@dataclass(frozen=True)
class PaceRange:
    minimum: int
    target: int
    maximum: int


@dataclass(frozen=True)
class OptimizationResult:
    route: list[dict[str, Any]]
    diagnostics: SolverDiagnostics
    score_breakdown: list[dict[str, Any]]
    quality_report: QualityReport


def pace_range(habit: str | None, max_per_day: int) -> PaceRange:
    text = (habit or "").lower()
    if any(token in text for token in ("慢", "轻松", "休闲", "不赶")):
        lower, upper = 2, 3
    elif any(token in text for token in ("紧凑", "特种兵", "尽量多", "多打卡", "赶行程")):
        lower, upper = 4, 5
    else:
        lower, upper = 3, 4
    upper = max(1, min(upper, max_per_day))
    lower = min(lower, upper)
    return PaceRange(lower, upper, upper)


def candidate_pool_limit(days: int) -> int:
    return max(12, min(24, max(1, days) * 8))


def _minute(text: str) -> int | None:
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", text)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return hour * 60 + minute


def format_minute(value: int) -> str:
    return f"{value // 60:02d}:{value % 60:02d}"


def period_for_minute(value: int) -> str:
    if value < 12 * 60:
        return "morning"
    if value < DINNER_WINDOW[0]:
        return "afternoon"
    return "evening"


def parse_open_interval(value: str | None, *, require_complete: bool = False) -> tuple[int, int] | None:
    """Extract a conservative same-day interval from common Amap text."""

    if not value:
        return None
    # A single stated daily range is verifiable. Split hours, weekday-specific
    # ranges, invalid clocks or explanatory uncertainty require clarification.
    match = re.fullmatch(
        r"\s*(?:(?:营业时间|开放时间|每日|每天)\s*[:：]?\s*)?"
        r"((?:[01]?\d|2[0-3]):[0-5]\d)\s*[-—–~～至]\s*"
        r"((?:[01]?\d|2[0-3]):[0-5]\d)\s*", value
    )
    if match is None:
        if require_complete:
            return None
        # Retain the established time bounds for ordinary scheduling when
        # supplier text also contains weekday/closure annotations. This does
        # not prove the complete hard opening requirement.
        clocks = re.findall(r"(?<!\d)([0-2]?\d:[0-5]\d)", value)
        if len(clocks) < 2:
            return None
        start, end = _minute(clocks[0]), _minute(clocks[1])
    else:
        start, end = _minute(match.group(1)), _minute(match.group(2))
    if start is None or end is None or start == end:
        return None
    if end < start:  # overnight venue; the planning day still ends at 21:00
        end += 24 * 60
    return max(DAY_START, start), min(DAY_END, end)


def allowed_start_bounds(candidate: Mapping[str, Any]) -> tuple[int, int] | None:
    """Return the feasible start interval after opening and appointment bounds."""
    duration = int(candidate["duration_min"])
    lower, upper = DAY_START, DAY_END - duration
    opening = parse_open_interval(candidate.get("open_time"))
    if opening:
        lower = max(lower, opening[0])
        upper = min(upper, opening[1] - duration)
    fixed = candidate.get("fixed_start_min")
    if fixed is not None:
        lower = max(lower, int(fixed))
        upper = min(upper, int(fixed))
    return (lower, upper) if lower <= upper else None


def is_closed_on_date(open_time: str | None, visit_date: date | None) -> bool:
    """Check explicit closed-day clauses for the visit date."""
    if not open_time or visit_date is None:
        return False
    labels = (
        ("周一", "星期一"), ("周二", "星期二"), ("周三", "星期三"),
        ("周四", "星期四"), ("周五", "星期五"), ("周六", "星期六"),
        ("周日", "周天", "星期日", "星期天"),
    )[visit_date.weekday()]
    clauses = re.split(r"[；;。\n]", open_time)
    return any(
        (any(label in clause for label in labels) or visit_date.isoformat() in clause)
        and any(token in clause for token in ("闭馆", "不开放", "休息"))
        for clause in clauses
    )


def _allowed_start_bounds(candidate: Mapping[str, Any]) -> tuple[int, int] | None:
    return allowed_start_bounds(candidate)


def _closed_on_date(open_time: str | None, visit_date: date | None) -> bool:
    return is_closed_on_date(open_time, visit_date)


def _scene_overlap(scene: str, start: int, end: int) -> str | None:
    windows: list[tuple[str, tuple[int, int]]] = []
    if scene in {"lunch", "either"}:
        windows.append(("lunch", LUNCH_WINDOW))
    if scene in {"dinner", "either"}:
        windows.append(("dinner", DINNER_WINDOW))
    for label, (window_start, window_end) in windows:
        if min(end, window_end) - max(start, window_start) >= MIN_MEAL_OVERLAP:
            return label
    return None


def meal_coverage_for_visit(scene: str, start: int, end: int) -> str | None:
    """Public deterministic coverage projection for manual edits/enrichments."""
    return _scene_overlap(scene, start, end)


def _route_distance(names: Sequence[str], candidates: Mapping[str, Mapping[str, Any]]) -> float:
    return sum(
        haversine_km(candidates[left]["location"], candidates[right]["location"])
        for left, right in zip(names, names[1:])
    )


def _earliest_schedule(
    ordered: Sequence[Mapping[str, Any]],
    visit_date: date | None = None,
    walking_time_minutes: Mapping[str, Mapping[str, int]] | None = None,
) -> list[tuple[int, int]] | None:
    schedule: list[tuple[int, int]] = []
    cursor = DAY_START
    for index, candidate in enumerate(ordered):
        if _closed_on_date(candidate.get("open_time"), visit_date):
            return None
        bounds = _allowed_start_bounds(candidate)
        if bounds is None:
            return None
        start = max(cursor, bounds[0])
        if start > bounds[1]:
            return None
        end = start + int(candidate["duration_min"])
        schedule.append((start, end))
        next_candidate = ordered[index + 1] if index + 1 < len(ordered) else None
        minutes = transfer_minutes(walking_time_minutes, candidate, next_candidate)
        cursor = end + int(minutes)
    return schedule


def _best_feasible_permutation(
    names: Sequence[str], candidates: Mapping[str, Mapping[str, Any]], visit_date: date | None = None,
    walking_time_minutes: Mapping[str, Mapping[str, int]] | None = None,
    transport_cost_cents: Mapping[str, Mapping[str, int]] | None = None,
) -> tuple[list[str], list[tuple[int, int]], float] | None:
    best = None
    for order in itertools.permutations(sorted(names)):
        positions = {name: index for index, name in enumerate(order)}
        if any(
            bool(candidates[name].get("must_be_first")) and positions[name] != 0
            for name in order
        ):
            continue
        if any(
            bool(candidates[name].get("must_be_last")) and positions[name] != len(order) - 1
            for name in order
        ):
            continue
        if any(
            positions[name] >= positions[successor]
            for name in order
            for successor in candidates[name].get("before_poi_names") or []
            if successor in positions
        ):
            continue
        ordered = [candidates[name] for name in order]
        schedule = _earliest_schedule(ordered, visit_date, walking_time_minutes)
        if schedule is None:
            continue
        distance = _route_distance(order, candidates)
        cost = sum(
            transport_cost_cents.get(walking_place_key(left), {}).get(walking_place_key(right), 0)
            for left, right in zip(ordered, ordered[1:])
        ) if transport_cost_cents is not None else 0
        key = (cost, round(distance, 9), order, schedule)
        if best is None or key[:3] < best[:3]:
            best = key
    if best is None:
        return None
    return list(best[2]), best[3], best[1]


def _base_candidate_score(candidate: Mapping[str, Any], profile: ScoringProfile) -> float:
    rewards = profile.rewards
    return (
        float(candidate.get("rating") or 0) / 5 * rewards.amap_rating
        + float(candidate.get("preference_match") or 0) * rewards.preference_match
        + float(candidate.get("representativeness") or 0) * rewards.representativeness
        + (rewards.meal_scene_match if candidate.get("meal_scene") != "none" else 0)
    )


class AttractionSubsetOptimizer:
    def __init__(
        self,
        profile_version: str = "balanced-v1",
        *,
        max_time_seconds: float = DEFAULT_SOLVER_SECONDS,
        random_seed: int = DEFAULT_RANDOM_SEED,
    ) -> None:
        self.profile = load_scoring_profile(profile_version)
        self.max_time_seconds = max_time_seconds
        self.random_seed = random_seed
        self.transport_cost_cents = {}
        self.transport_budget_cents = None

    def solve(
        self,
        candidates: Sequence[OptimizerCandidate | Mapping[str, Any]],
        *,
        days: int,
        habit_preference: str | None = None,
        max_per_day: int = 5,
        weather_by_day: Mapping[int, Mapping[str, Any]] | None = None,
        travel_start_date: date | None = None,
        user_daily_min: int | None = None,
        user_daily_max: int | None = None,
        walking_time_minutes: Mapping[str, Mapping[str, int]] | None = None,
        transport_cost_cents: Mapping[str, Mapping[str, int]] | None = None,
        transport_budget_cents: int | None = None,
    ) -> OptimizationResult:
        self.transport_cost_cents = transport_cost_cents or {}
        self.transport_budget_cents = transport_budget_cents
        normalized = [
            item if isinstance(item, OptimizerCandidate) else OptimizerCandidate.model_validate(item)
            for item in candidates
        ]
        if not normalized or days < 1:
            raise OptimizationFailure("candidate pool and positive days are required")
        pace = pace_range(habit_preference, max_per_day)
        effective_max = min(
            max_per_day,
            user_daily_max
            if user_daily_max is not None
            else max(pace.maximum, user_daily_min or 0),
        )
        if user_daily_min is not None and user_daily_min > effective_max:
            raise OptimizationFailure("user daily minimum exceeds the hard daily maximum")
        pace = replace(
            pace,
            maximum=max(1, effective_max),
            target=min(max(pace.target, user_daily_min or 0), max(1, effective_max)),
        )
        system_minimum = max(pace.minimum, user_daily_min or 0)
        pace = replace(pace, minimum=system_minimum)
        self.attempts = []
        first = self._solve_cp_sat(normalized, days, pace, weather_by_day, travel_start_date, walking_time_minutes, relaxed=False)
        if first is not None:
            first.diagnostics.attempts = list(self.attempts)
            return first
        relaxed_pace = replace(pace, minimum=max(1, user_daily_min or 0))
        second = self._solve_cp_sat(normalized, days, relaxed_pace, weather_by_day, travel_start_date, walking_time_minutes, relaxed=True)
        if second is not None:
            second.diagnostics.attempts = list(self.attempts)
            return second
        fallback = self._fallback(normalized, days, relaxed_pace, weather_by_day, travel_start_date, walking_time_minutes)
        if fallback is None:
            raise OptimizationFailure("no feasible route after daily-min relaxation and fallback")
        fallback.diagnostics.attempts = list(self.attempts)
        return fallback

    def _solve_cp_sat(
        self,
        candidates: list[OptimizerCandidate],
        days: int,
        pace: PaceRange,
        weather_by_day: Mapping[int, Mapping[str, Any]] | None,
        travel_start_date: date | None,
        walking_time_minutes: Mapping[str, Mapping[str, int]] | None,
        *,
        relaxed: bool,
    ) -> OptimizationResult | None:
        if cp_model is None:
            self.attempts.append({"status": "UNAVAILABLE", "relaxed": relaxed, "elapsed_ms": 0})
            return None
        started = time.perf_counter()
        model = cp_model.CpModel()
        count = len(candidates)
        x: dict[tuple[int, int], Any] = {}
        selected: dict[int, Any] = {}
        starts: dict[tuple[int, int], Any] = {}
        coverage: dict[tuple[int, int, str], Any] = {}
        arcs: dict[tuple[int, int, int], Any] = {}
        objective_terms: list[Any] = []
        scale = self.profile.objective_scale * 60

        for index, candidate in enumerate(candidates):
            selected[index] = model.NewBoolVar(f"selected_{index}")
            for day in range(days):
                x[index, day] = model.NewBoolVar(f"assigned_{index}_{day}")
            model.Add(selected[index] == sum(x[index, day] for day in range(days)))
            if candidate.must_visit:
                model.Add(selected[index] == 1)
            if candidate.fixed_day is not None:
                target = candidate.fixed_day - 1
                if not 0 <= target < days:
                    return None
                model.Add(x[index, target] == 1)
            rating_raw = int(round(float(candidate.rating or 0) * 100))
            objective_terms.append(selected[index] * int(round(self.profile.rewards.amap_rating * scale / 500)) * rating_raw)
            preference_raw = int(round(candidate.preference_match * 100))
            represent_raw = int(round(candidate.representativeness * 100))
            objective_terms.append(selected[index] * int(round(self.profile.rewards.preference_match * scale / 100)) * preference_raw)
            objective_terms.append(selected[index] * int(round(self.profile.rewards.representativeness * scale / 100)) * represent_raw)

        for day in range(days):
            daily_count = sum(x[index, day] for index in range(count))
            model.Add(daily_count >= pace.minimum)
            model.Add(daily_count <= pace.maximum)
            objective_terms.append(daily_count * int(round(self.profile.rewards.daily_target_fill * scale)))

            # Open path via AddCircuit: end -> start is fixed and unselected POIs
            # take their self loops.  All other active arcs form one day route.
            start_node, end_node = 0, 1
            circuit: list[tuple[int, int, Any]] = [(end_node, start_node, model.NewConstant(1))]
            empty = model.NewBoolVar(f"empty_{day}")
            circuit.append((start_node, end_node, empty))
            for i in range(count):
                node = i + 2
                circuit.append((node, node, x[i, day].Not()))
                start_arc = model.NewBoolVar(f"arc_start_{i}_{day}")
                end_arc = model.NewBoolVar(f"arc_end_{i}_{day}")
                arcs[-1, i, day] = start_arc
                arcs[i, -2, day] = end_arc
                circuit.extend(((start_node, node, start_arc), (node, end_node, end_arc)))
                for j in range(count):
                    if i == j:
                        continue
                    arc = model.NewBoolVar(f"arc_{i}_{j}_{day}")
                    arcs[i, j, day] = arc
                    circuit.append((node, j + 2, arc))
            model.AddCircuit(circuit)

            for i, candidate in enumerate(candidates):
                if candidate.must_be_first:
                    model.Add(arcs[-1, i, day] == x[i, day])
                if candidate.must_be_last:
                    model.Add(arcs[i, -2, day] == x[i, day])

            for i, candidate in enumerate(candidates):
                bounds = _allowed_start_bounds(candidate.model_dump())
                visit_date = travel_start_date + timedelta(days=day) if travel_start_date else None
                if bounds is None or _closed_on_date(candidate.open_time, visit_date):
                    model.Add(x[i, day] == 0)
                    lower, upper = DAY_START, DAY_START
                else:
                    lower, upper = bounds
                start_var = model.NewIntVar(DAY_START, DAY_END, f"start_{i}_{day}")
                starts[i, day] = start_var
                model.Add(start_var >= lower).OnlyEnforceIf(x[i, day])
                model.Add(start_var <= upper).OnlyEnforceIf(x[i, day])
                model.Add(start_var == DAY_START).OnlyEnforceIf(x[i, day].Not())
                duration = candidate.duration_min

                for meal, window in (("lunch", LUNCH_WINDOW), ("dinner", DINNER_WINDOW)):
                    cover = model.NewBoolVar(f"coverage_{i}_{day}_{meal}")
                    coverage[i, day, meal] = cover
                    model.Add(cover <= x[i, day])
                    allowed = candidate.meal_scene in {meal, "either"}
                    if not allowed:
                        model.Add(cover == 0)
                    else:
                        model.Add(start_var <= window[1] - MIN_MEAL_OVERLAP).OnlyEnforceIf(cover)
                        model.Add(start_var + duration >= window[0] + MIN_MEAL_OVERLAP).OnlyEnforceIf(cover)
                model.Add(coverage[i, day, "lunch"] + coverage[i, day, "dinner"] <= 1)
                covered = coverage[i, day, "lunch"] + coverage[i, day, "dinner"]
                objective_terms.append(covered * int(round(self.profile.rewards.meal_scene_match * scale)))
                if candidate.meal_scene != "none":
                    objective_terms.append(
                        (x[i, day] - covered) * -int(round(self.profile.penalties.meal_scene_miss * scale))
                    )

                preferred_match = model.NewBoolVar(f"period_match_{i}_{day}")
                model.Add(preferred_match <= x[i, day])
                preferred = candidate.preferred_period
                if preferred == "any":
                    model.Add(preferred_match == x[i, day])
                else:
                    lower_period, upper_period = {
                        "morning": (DAY_START, 12 * 60 - 1),
                        "afternoon": (12 * 60, DINNER_WINDOW[0] - 1),
                        "evening": (DINNER_WINDOW[0], DAY_END),
                    }[preferred]
                    model.Add(start_var >= lower_period).OnlyEnforceIf(preferred_match)
                    model.Add(start_var <= upper_period).OnlyEnforceIf(preferred_match)
                objective_terms.append(
                    (x[i, day] - preferred_match)
                    * -int(round(self.profile.penalties.preferred_period_miss * scale))
                )
                if self._weather_mismatch(candidate, (weather_by_day or {}).get(day + 1)):
                    objective_terms.append(
                        x[i, day] * -int(round(self.profile.penalties.weather_mismatch * scale))
                    )

            for i, left in enumerate(candidates):
                for j, right in enumerate(candidates):
                    if i == j:
                        continue
                    arc = arcs[i, j, day]
                    travel_minutes = int(transfer_minutes(walking_time_minutes, left, right))
                    model.Add(
                        starts[j, day] >= starts[i, day] + left.duration_min + travel_minutes
                    ).OnlyEnforceIf(arc)
                    distance_units = int(round(haversine_km(left.location, right.location) * 10))
                    distance_coeff = int(round(self.profile.penalties.distance_per_km * scale / 10))
                    objective_terms.append(arc * -distance_coeff * distance_units)
                    if left.cluster_id != right.cluster_id:
                        objective_terms.append(
                            arc * -int(round(self.profile.penalties.cross_cluster_arc * scale))
                        )
                    left_fatigue = bool({"high_fatigue", "高体力"} & set(left.semantic_tags))
                    right_fatigue = bool({"high_fatigue", "高体力"} & set(right.semantic_tags))
                    if left_fatigue and right_fatigue:
                        objective_terms.append(
                            arc * -int(round(self.profile.penalties.consecutive_high_fatigue * scale))
                        )
                    wait = model.NewIntVar(0, DAY_END - DAY_START, f"wait_{i}_{j}_{day}")
                    model.Add(wait == 0).OnlyEnforceIf(arc.Not())
                    model.Add(
                        wait == starts[j, day] - starts[i, day] - left.duration_min - travel_minutes
                    ).OnlyEnforceIf(arc)
                    wait_coeff = int(round(self.profile.penalties.waiting_per_hour * scale / 60))
                    objective_terms.append(wait * -wait_coeff)

            name_to_index = {candidate.poi_name: index for index, candidate in enumerate(candidates)}
            for i, candidate in enumerate(candidates):
                for successor_name in candidate.before_poi_names:
                    j = name_to_index.get(successor_name)
                    if j is None:
                        continue
                    model.Add(
                        starts[j, day] >= starts[i, day] + candidate.duration_min + int(transfer_minutes(walking_time_minutes, candidate, candidates[j]))
                    ).OnlyEnforceIf([x[i, day], x[j, day]])

            categories = sorted({candidate.category for candidate in candidates})
            for category in categories:
                present = model.NewBoolVar(f"category_{category}_{day}")
                members = [x[i, day] for i, candidate in enumerate(candidates) if candidate.category == category]
                model.Add(sum(members) >= present)
                for member in members:
                    model.Add(present >= member)
                objective_terms.append(present * int(round(self.profile.rewards.category_diversity * scale)))

        loads = []
        for day in range(days):
            load = model.NewIntVar(0, DAY_END - DAY_START, f"load_{day}")
            model.Add(load == sum(candidates[i].duration_min * x[i, day] for i in range(count)))
            loads.append(load)
        if len(loads) > 1:
            max_load = model.NewIntVar(0, DAY_END - DAY_START, "max_load")
            min_load = model.NewIntVar(0, DAY_END - DAY_START, "min_load")
            model.AddMaxEquality(max_load, loads)
            model.AddMinEquality(min_load, loads)
            imbalance_coeff = int(round(self.profile.penalties.daily_load_imbalance_per_hour * scale / 60))
            objective_terms.append((max_load - min_load) * -imbalance_coeff)

        # Cross-day precedence: a predecessor can share a day (time constraint
        # above) or appear on an earlier day, never on a later day.
        name_to_index = {candidate.poi_name: index for index, candidate in enumerate(candidates)}
        for i, candidate in enumerate(candidates):
            for successor_name in candidate.before_poi_names:
                j = name_to_index.get(successor_name)
                if j is None:
                    continue
                for predecessor_day in range(days):
                    for successor_day in range(predecessor_day):
                        model.AddBoolOr([x[i, predecessor_day].Not(), x[j, successor_day].Not()])

        if self.transport_budget_cents is not None:
            model.Add(sum(arcs[i, j, day] * self._transfer_cost(left, right)
                          for day in range(days) for i, left in enumerate(candidates)
                          for j, right in enumerate(candidates) if i != j) <= self.transport_budget_cents)
        model.Maximize(sum(objective_terms))
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = self.max_time_seconds
        solver.parameters.num_search_workers = 1
        solver.parameters.random_seed = self.random_seed
        solver.parameters.randomize_search = False
        status = solver.Solve(model)
        self.attempts.append({"status": solver.StatusName(status), "relaxed": relaxed,
                              "elapsed_ms": int((time.perf_counter() - started) * 1000)})
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return None

        candidate_map = {candidate.poi_name: candidate.model_dump() for candidate in candidates}
        route: list[dict[str, Any]] = []
        for day in range(days):
            next_index = next(
                (i for i in range(count) if solver.Value(arcs[-1, i, day])), None
            )
            ordered: list[int] = []
            while next_index is not None and next_index not in ordered:
                ordered.append(next_index)
                successor = next(
                    (j for j in range(count) if j != next_index and solver.Value(arcs[next_index, j, day])),
                    None,
                )
                next_index = successor
            spots: list[dict[str, Any]] = []
            for i in ordered:
                start = solver.Value(starts[i, day])
                end = start + candidates[i].duration_min
                meal_coverage = next(
                    (meal for meal in ("lunch", "dinner") if solver.Value(coverage[i, day, meal])),
                    None,
                )
                spots.append(self._spot(candidates[i], start, end, meal_coverage))
            route.append({"day": day + 1, "theme": self._theme(spots), "spots": spots})

        self._annotate_transfers(route, candidate_map, walking_time_minutes)
        score, breakdown = evaluate_itinerary(
            route,
            candidate_map,
            self.profile,
            daily_target=pace.target,
            weather_by_day=weather_by_day,
        )
        linear_objective = solver.ObjectiveValue() / scale
        bound = solver.BestObjectiveBound() / scale
        gap = abs(linear_objective - bound) / max(1.0, abs(linear_objective))
        diagnostics = SolverDiagnostics(
            status="OPTIMAL" if status == cp_model.OPTIMAL else "FEASIBLE",
            objective=round(linear_objective, 6),
            best_bound=round(bound, 6),
            gap=round(gap, 8),
            elapsed_ms=max(0, int(round((time.perf_counter() - started) * 1000))),
            scoring_profile=self.profile.version,
            random_seed=self.random_seed,
            relaxed_constraints=["RELAXED_DAILY_MIN"] if relaxed else [],
        )
        quality = validate_solution(
            route,
            candidates,
            diagnostics,
            self.profile,
            pace,
            weather_by_day=weather_by_day,
            allow_relaxed_min=relaxed,
            travel_start_date=travel_start_date,
            walking_time_minutes=walking_time_minutes,
        )
        return OptimizationResult(route, diagnostics, [item.model_dump() for item in breakdown], quality)

    def _fallback(
        self,
        candidates: list[OptimizerCandidate],
        days: int,
        pace: PaceRange,
        weather_by_day: Mapping[int, Mapping[str, Any]] | None,
        travel_start_date: date | None,
        walking_time_minutes: Mapping[str, Mapping[str, int]] | None,
    ) -> OptimizationResult | None:
        started = time.perf_counter()
        ordered = sorted(
            candidates,
            key=lambda item: (
                not item.must_visit,
                -_base_candidate_score(item.model_dump(), self.profile),
                item.poi_name,
            ),
        )
        candidate_map = {candidate.poi_name: candidate.model_dump() for candidate in candidates}
        objects = {item.poi_name: item for item in candidates}
        schedules = {}

        def scheduled(day, names):
            key = (day, tuple(sorted(names)))
            if key not in schedules:
                visit_date = travel_start_date + timedelta(days=day) if travel_start_date else None
                # Choose the cheapest feasible direction before pruning the whole
                # group against the trip allowance. Reverse fares may differ.
                schedules[key] = _best_feasible_permutation(
                    names, candidate_map, visit_date, walking_time_minutes,
                    self.transport_cost_cents if self.transport_budget_cents is not None else None,
                )
            return schedules[key]

        def rank(assignment):
            names = [name for group in assignment for name in group]
            return (
                -sum(min(len(group), pace.minimum) for group in assignment),
                -len(names),
                -sum(_base_candidate_score(candidate_map[name], self.profile) for name in names),
                sum(scheduled(day, group)[2] for day, group in enumerate(assignment) if group),
                assignment,
            )

        def cost(assignment):
            return sum(self._transfer_cost(objects[left], objects[right])
                       for day, group in enumerate(assignment) if group
                       for left, right in zip(scheduled(day, group)[0], scheduled(day, group)[0][1:]))

        # Keep alternatives which omit optional POIs as well as different days.
        # Unlike the former fixed partition, every insertion is time-feasible.
        beam = [tuple(() for _ in range(days))]
        for candidate in ordered:
            expanded = set()
            for assignment in beam:
                if not candidate.must_visit:
                    expanded.add(assignment)
                eligible = [candidate.fixed_day - 1] if candidate.fixed_day else range(days)
                for day in eligible:
                    if not 0 <= day < days or len(assignment[day]) >= pace.maximum:
                        continue
                    group = tuple(sorted((*assignment[day], candidate.poi_name)))
                    if scheduled(day, group) is None:
                        continue
                    revised = list(assignment)
                    revised[day] = group
                    if self.transport_budget_cents is not None and cost(revised) > self.transport_budget_cents:
                        continue
                    expanded.add(tuple(revised))
            beam = sorted(expanded, key=rank)[:24]
            if not beam:
                return None
        for assignment in beam:
            if any(len(group) < pace.minimum for group in assignment):
                continue
            route = []
            for day, group in enumerate(assignment):
                names, schedule, _ = scheduled(day, group)
                spots = [self._spot(objects[name], start, end, _scene_overlap(objects[name].meal_scene, start, end))
                         for name, (start, end) in zip(names, schedule)]
                route.append({"day": day + 1, "theme": self._theme(spots), "spots": spots})
            self._annotate_transfers(route, candidate_map, walking_time_minutes)
            score, _ = evaluate_itinerary(route, candidate_map, self.profile, daily_target=pace.target, weather_by_day=weather_by_day)
            probe = SolverDiagnostics(status="FALLBACK", objective=score, elapsed_ms=0,
                                      scoring_profile=self.profile.version, random_seed=self.random_seed)
            if validate_solution(route, candidates, probe, self.profile, pace, weather_by_day=weather_by_day,
                                 travel_start_date=travel_start_date, walking_time_minutes=walking_time_minutes).passed:
                break
        else:
            return None
        self._annotate_transfers(route, candidate_map, walking_time_minutes)
        score, breakdown = evaluate_itinerary(
            route, candidate_map, self.profile, daily_target=pace.target, weather_by_day=weather_by_day
        )
        diagnostics = SolverDiagnostics(
            status="FALLBACK",
            objective=score,
            best_bound=None,
            gap=None,
            elapsed_ms=max(0, int(round((time.perf_counter() - started) * 1000))),
            scoring_profile=self.profile.version,
            random_seed=self.random_seed,
            relaxed_constraints=["RELAXED_DAILY_MIN"],
            fallback_used=True,
        )
        quality = validate_solution(
            route, candidates, diagnostics, self.profile, pace,
            weather_by_day=weather_by_day, allow_relaxed_min=True,
            travel_start_date=travel_start_date,
            walking_time_minutes=walking_time_minutes,
        )
        return OptimizationResult(route, diagnostics, [item.model_dump() for item in breakdown], quality)

    @staticmethod
    def _annotate_transfers(route, candidates, minutes):
        for day in route:
            previous = None
            for spot in day["spots"]:
                current = candidates[spot["name"]]
                spot["transfer_from_previous_min"] = transfer_minutes(minutes, previous, current)
                previous = current

    def _transfer_cost(self, origin, destination):
        return self.transport_cost_cents.get(walking_place_key(origin), {}).get(walking_place_key(destination), 0)

    @staticmethod
    def _weather_mismatch(candidate: OptimizerCandidate, weather: Mapping[str, Any] | None) -> bool:
        if not weather:
            return False
        text = " ".join(str(weather.get(key) or "") for key in ("dayweather", "nightweather", "weather"))
        wet = any(token in text for token in ("雨", "雪", "雷", "storm"))
        return wet and bool({"outdoor", "户外", "hiking", "徒步"} & set(candidate.semantic_tags))

    @staticmethod
    def _spot(candidate: OptimizerCandidate, start: int, end: int, coverage: str | None) -> dict[str, Any]:
        return {
            "name": candidate.poi_name,
            "period": period_for_minute(start),
            "start_time": format_minute(start),
            "end_time": format_minute(end),
            "start_min": start,
            "end_min": end,
            "meal_scene": candidate.meal_scene,
            "meal_coverage": coverage,
            "semantic_source": candidate.semantic_source,
        }

    @staticmethod
    def _theme(spots: Sequence[Mapping[str, Any]]) -> str:
        categories = list(dict.fromkeys(str(spot.get("name") or "") for spot in spots[:2]))
        return "与".join(categories) if categories else "城市漫游"


def validate_solution(
    route: list[dict[str, Any]],
    candidates: Sequence[OptimizerCandidate],
    diagnostics: SolverDiagnostics,
    profile: ScoringProfile,
    pace: PaceRange,
    *,
    weather_by_day: Mapping[int, Mapping[str, Any]] | None = None,
    allow_relaxed_min: bool = False,
    travel_start_date: date | None = None,
    walking_time_minutes: Mapping[str, Mapping[str, int]] | None = None,
) -> QualityReport:
    """Independently validate hard constraints, objective, and day ordering."""

    candidate_map = {candidate.poi_name: candidate.model_dump() for candidate in candidates}
    violations: list[QualityViolation] = []
    seen: set[str] = set()
    for day in route:
        day_no = int(day.get("day", 0))
        spots = day.get("spots") or []
        minimum = 1 if allow_relaxed_min else pace.minimum
        if not minimum <= len(spots) <= pace.maximum:
            violations.append(QualityViolation(
                code="DAILY_COUNT", message=f"Day {day_no} count {len(spots)} outside {minimum}-{pace.maximum}", day=day_no
            ))
        previous_end: int | None = None
        previous_candidate: Mapping[str, Any] | None = None
        day_names = [str(spot.get("name") or "") for spot in spots]
        for spot in spots:
            name = str(spot.get("name") or "")
            if name not in candidate_map:
                violations.append(QualityViolation(code="UNKNOWN_POI", message=f"{name} not in candidate pool", day=day_no, poi_name=name))
                continue
            if name in seen:
                violations.append(QualityViolation(code="DUPLICATE_POI", message=f"{name} appears more than once", day=day_no, poi_name=name))
            seen.add(name)
            candidate = candidate_map[name]
            start = int(spot.get("start_min", _minute(str(spot.get("start_time") or "")) or -1))
            end = int(spot.get("end_min", _minute(str(spot.get("end_time") or "")) or -1))
            if start < DAY_START or end > DAY_END or end - start != int(candidate["duration_min"]):
                violations.append(QualityViolation(code="TIME_WINDOW", message=f"invalid time for {name}", day=day_no, poi_name=name))
            opening = parse_open_interval(candidate.get("open_time"))
            visit_date = travel_start_date + timedelta(days=day_no - 1) if travel_start_date else None
            if _closed_on_date(candidate.get("open_time"), visit_date) or (
                opening is not None and (start < opening[0] or end > opening[1])
            ):
                violations.append(QualityViolation(code="OPENING_TIME", message=f"{name} is outside its available interval", day=day_no, poi_name=name))
            if candidate.get("fixed_start_min") is not None and start != int(candidate["fixed_start_min"]):
                violations.append(QualityViolation(code="FIXED_START", message=f"{name} is not at its fixed start", day=day_no, poi_name=name))
            if previous_end is not None:
                transfer = int(transfer_minutes(walking_time_minutes, previous_candidate, candidate))
                if start < previous_end + transfer:
                    violations.append(QualityViolation(code="TRANSFER_BUFFER", message=f"{name} starts before the {transfer}-minute transfer", day=day_no, poi_name=name))
            previous_end = end
            previous_candidate = candidate
            if candidate.get("fixed_day") is not None and int(candidate["fixed_day"]) != day_no:
                violations.append(QualityViolation(code="FIXED_DAY", message=f"{name} is assigned to the wrong day", day=day_no, poi_name=name))
            coverage = spot.get("meal_coverage")
            if coverage and coverage != _scene_overlap(str(candidate.get("meal_scene")), start, end):
                violations.append(QualityViolation(code="MEAL_COVERAGE", message=f"invalid meal coverage for {name}", day=day_no, poi_name=name))
            if candidate.get("must_be_first") and day_names and day_names[0] != name:
                violations.append(QualityViolation(code="FIRST_STOP", message=f"{name} is not first", day=day_no, poi_name=name))
            if candidate.get("must_be_last") and day_names and day_names[-1] != name:
                violations.append(QualityViolation(code="LAST_STOP", message=f"{name} is not last", day=day_no, poi_name=name))
    for candidate in candidates:
        if candidate.must_visit and candidate.poi_name not in seen:
            violations.append(QualityViolation(code="MISSING_MUST_VISIT", message=f"must-visit {candidate.poi_name} was omitted", poi_name=candidate.poi_name))
        if candidate.poi_name in seen:
            source_day = next(
                (int(day["day"]) for day in route if candidate.poi_name in [s["name"] for s in day.get("spots") or []]),
                None,
            )
            source_position = next(
                (index for day in route for index, spot in enumerate(day.get("spots") or []) if spot["name"] == candidate.poi_name),
                None,
            )
            for successor in candidate.before_poi_names:
                if successor not in seen:
                    continue
                successor_day = next(int(day["day"]) for day in route if successor in [s["name"] for s in day.get("spots") or []])
                successor_position = next(index for day in route for index, spot in enumerate(day.get("spots") or []) if spot["name"] == successor)
                if source_day is not None and (source_day > successor_day or (source_day == successor_day and source_position is not None and source_position >= successor_position)):
                    violations.append(QualityViolation(code="PRECEDENCE", message=f"{candidate.poi_name} must precede {successor}", poi_name=candidate.poi_name))

    recomputed, _ = evaluate_itinerary(
        route, candidate_map, profile, daily_target=pace.target, weather_by_day=weather_by_day
    )
    delta = None if diagnostics.objective is None else abs(recomputed - diagnostics.objective)
    if delta is not None and delta > 1e-6:
        violations.append(QualityViolation(code="OBJECTIVE_MISMATCH", message=f"objective delta={delta:.8f}"))

    ratios: dict[int, float] = {}
    for day in route:
        names = [spot["name"] for spot in day.get("spots") or []]
        if len(names) < 2:
            ratios[int(day["day"])] = 1.0
            continue
        visit_date = travel_start_date + timedelta(days=int(day["day"]) - 1) if travel_start_date else None
        shortest = _best_feasible_permutation(names, candidate_map, visit_date, walking_time_minutes)
        if shortest is None:
            violations.append(QualityViolation(code="NO_FEASIBLE_ORDER", message=f"Day {day['day']} has no feasible order", day=int(day["day"])))
            continue
        selected_distance = _route_distance(names, candidate_map)
        shortest_distance = shortest[2]
        ratio = 1.0 if shortest_distance <= 1e-9 else selected_distance / shortest_distance
        ratios[int(day["day"])] = round(ratio, 6)
        # Distance is a soft objective alongside opening hours, meal coverage,
        # waiting time, and preference fit. Keep this ratio as a diagnostic;
        # it must not reject a feasible, higher-scoring optimizer route.

    return QualityReport(
        passed=not violations,
        violations=violations,
        recomputed_objective=recomputed,
        solver_objective_delta=delta,
        daily_order_ratios=ratios,
    )

"""Versioned, independently reproducible scoring for attraction optimization."""

from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.planning.helpers import haversine_km
from app.planning.schemas import ScoreBreakdownItem


class MetricWeights(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    amap_rating: float = Field(ge=0)
    preference_match: float = Field(ge=0)
    representativeness: float = Field(ge=0)
    category_diversity: float = Field(ge=0)
    daily_target_fill: float = Field(ge=0)
    meal_scene_match: float = Field(ge=0)


class PenaltyWeights(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    distance_per_km: float = Field(ge=0)
    preferred_period_miss: float = Field(ge=0)
    meal_scene_miss: float = Field(ge=0)
    weather_mismatch: float = Field(ge=0)
    consecutive_high_fatigue: float = Field(ge=0)
    cross_cluster_arc: float = Field(ge=0)
    waiting_per_hour: float = Field(ge=0)
    daily_load_imbalance_per_hour: float = Field(ge=0)


class ScoringProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: str
    objective_scale: int = Field(default=1000, ge=100, le=1_000_000)
    rewards: MetricWeights
    penalties: PenaltyWeights

    @model_validator(mode="after")
    def _known_version(self) -> "ScoringProfile":
        if not self.version.strip():
            raise ValueError("profile version is required")
        return self

    def coefficient(self, component: str, *, penalty: bool = False) -> int:
        source = self.penalties if penalty else self.rewards
        return int(round(float(getattr(source, component)) * self.objective_scale))


_PROFILE_DIR = Path(__file__).with_name("scoring_profiles")


@lru_cache(maxsize=16)
def load_scoring_profile(version: str = "balanced-v1") -> ScoringProfile:
    path = _PROFILE_DIR / f"{version}.yaml"
    if not path.is_file():
        raise ValueError(f"unknown scoring profile: {version}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return ScoringProfile.model_validate(raw)


@dataclass(frozen=True)
class LinearScoreTerm:
    """A CP-SAT-ready coefficient plus a model-owned linear expression."""

    component: str
    coefficient: int
    expression: Any


def build_linear_term(
    profile: ScoringProfile,
    component: str,
    expression: Any,
    *,
    penalty: bool = False,
    unit_scale: int = 1,
) -> LinearScoreTerm:
    """Shared linear-expression builder used by every solver component."""

    coefficient = profile.coefficient(component, penalty=penalty)
    coefficient = int(round(coefficient / max(1, unit_scale)))
    return LinearScoreTerm(component, -coefficient if penalty else coefficient, expression)


def _period_for_minute(minute: int) -> str:
    if minute < 12 * 60:
        return "morning"
    if minute < 17 * 60 + 30:
        return "afternoon"
    return "evening"


def _weather_mismatch(candidate: Mapping[str, Any], weather: Mapping[str, Any] | None) -> bool:
    if not weather:
        return False
    tags = {str(tag).lower() for tag in candidate.get("semantic_tags") or []}
    conditions = " ".join(str(weather.get(key) or "") for key in ("dayweather", "nightweather", "weather"))
    wet = any(token in conditions for token in ("雨", "雪", "雷", "storm"))
    return wet and bool(tags & {"outdoor", "户外", "hiking", "徒步"})


def evaluate_itinerary(
    route: list[dict[str, Any]],
    candidates: Mapping[str, Mapping[str, Any]],
    profile: ScoringProfile,
    *,
    daily_target: int,
    weather_by_day: Mapping[int, Mapping[str, Any]] | None = None,
) -> tuple[float, list[ScoreBreakdownItem]]:
    """Recompute every objective component without consulting solver variables."""

    raw: dict[str, float] = {
        "amap_rating": 0,
        "preference_match": 0,
        "representativeness": 0,
        "category_diversity": 0,
        "daily_target_fill": 0,
        "meal_scene_match": 0,
        "distance_per_km": 0,
        "preferred_period_miss": 0,
        "meal_scene_miss": 0,
        "weather_mismatch": 0,
        "consecutive_high_fatigue": 0,
        "cross_cluster_arc": 0,
        "waiting_per_hour": 0,
        "daily_load_imbalance_per_hour": 0,
    }
    loads: list[float] = []
    for day in route:
        spots = day.get("spots") or []
        raw["daily_target_fill"] += min(len(spots), daily_target)
        raw["category_diversity"] += len({candidates[s["name"]].get("category", "other") for s in spots})
        day_load = 0.0
        for index, spot in enumerate(spots):
            candidate = candidates[spot["name"]]
            rating = candidate.get("rating")
            raw["amap_rating"] += round(float(rating or 0) * 100) / 500.0
            raw["preference_match"] += round(float(candidate.get("preference_match") or 0) * 100) / 100.0
            raw["representativeness"] += round(float(candidate.get("representativeness") or 0) * 100) / 100.0
            start = int(spot.get("start_min", 0))
            end = int(spot.get("end_min", start))
            day_load += max(0, end - start)
            coverage = spot.get("meal_coverage")
            if coverage:
                raw["meal_scene_match"] += 1
            elif candidate.get("meal_scene") != "none":
                raw["meal_scene_miss"] += 1
            preferred = candidate.get("preferred_period", "any")
            if preferred != "any" and _period_for_minute(start) != preferred:
                raw["preferred_period_miss"] += 1
            if _weather_mismatch(candidate, (weather_by_day or {}).get(int(day.get("day", 0)))):
                raw["weather_mismatch"] += 1
            if index:
                previous = spots[index - 1]
                previous_candidate = candidates[previous["name"]]
                raw["distance_per_km"] += round(haversine_km(
                    previous_candidate["location"], candidate["location"]
                ) * 10) / 10.0
                if previous_candidate.get("cluster_id") != candidate.get("cluster_id"):
                    raw["cross_cluster_arc"] += 1
                if {"high_fatigue", "高体力"} & set(candidate.get("semantic_tags") or []) and {
                    "high_fatigue", "高体力"
                } & set(previous_candidate.get("semantic_tags") or []):
                    raw["consecutive_high_fatigue"] += 1
                previous_end = int(previous.get("end_min", 0))
                transfer = int(spot.get("transfer_from_previous_min", 20))
                raw["waiting_per_hour"] += max(0, start - previous_end - transfer) / 60.0
        loads.append(day_load / 60.0)
    if loads:
        # Linear CP-SAT formulation and offline evaluator share the same stable
        # definition: the spread between the heaviest and lightest day.
        raw["daily_load_imbalance_per_hour"] = max(loads) - min(loads)

    items: list[ScoreBreakdownItem] = []
    total = 0.0
    reward_data = profile.rewards.model_dump()
    penalty_data = profile.penalties.model_dump()
    for component, value in raw.items():
        penalty = component in penalty_data
        weight = float((penalty_data if penalty else reward_data)[component])
        contribution = value * weight * (-1 if penalty else 1)
        if math.isclose(contribution, 0.0, abs_tol=1e-12):
            contribution = 0.0
        total += contribution
        items.append(ScoreBreakdownItem(
            component=component,
            raw_value=round(value, 6),
            weight=weight,
            contribution=round(contribution, 6),
            explanation=("penalty" if penalty else "reward"),
        ))
    return round(total, 6), items

"""Rule-first attraction semantics and authoritative POI linkage."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

from app.planning.schemas import CandidateAttraction, OptimizerCandidate


@dataclass(frozen=True)
class MealSceneInference:
    meal_scene: str
    evidence: tuple[str, ...]


_DINNER_NAME_PATTERNS = (
    "夜市", "夜间美食街", "夜宵街", "不夜城", "夜生活街区",
)
_LUNCH_NAME_PATTERNS = (
    "午市", "早午市集", "午间市集", "白昼市集",
)
_EITHER_NAME_PATTERNS = (
    "小吃街", "美食街", "餐饮街", "美食城", "食集", "美食广场",
)
_DINNER_TYPE_PATTERNS = ("夜市", "夜间餐饮")
_EITHER_TYPE_PATTERNS = ("餐饮街区", "餐饮服务", "特色商业街")
_FOOD_FOCUS_TERMS = ("吃吃喝喝为主", "美食为主", "餐饮为主", "美食打卡", "专门吃", "逛吃")


def is_food_street(poi: dict[str, Any]) -> bool:
    """Whether a POI is primarily a meal-scene attraction (not a normal sight)."""
    return semantic_category(poi) == "food_street"


def has_food_focus(
    constraints: Iterable[dict[str, Any]], food_preference: str | None = None
) -> bool:
    """Only an explicit trip focus may promote food streets into core stops.

    Cuisine/diet requests such as "want hotpot" are intentionally insufficient:
    they guide meal selection, not the number of food-street attractions.
    """
    texts = [str(food_preference or "")]
    texts.extend(str(item.get("value_text") or "") for item in constraints)
    return any(term in text for text in texts for term in _FOOD_FOCUS_TERMS)


def is_featured_food_street(poi: dict[str, Any]) -> bool:
    """A strong city-food landmark may remain as one optional core stop."""
    try:
        rating = float(poi.get("rating") or poi.get("biz_ext", {}).get("rating") or 0)
    except (TypeError, ValueError, AttributeError):
        rating = 0
    return is_food_street(poi) and rating >= 4.5


def infer_meal_scene(poi: dict[str, Any]) -> MealSceneInference:
    """Infer meal semantics from Amap type/typecode and name, deterministically.

    A specific name wins over a broad Amap category.  The returned evidence is
    stored with the itinerary so false positives can be evaluated later.
    """

    name = str(poi.get("name") or poi.get("poi_name") or "").strip()
    poi_type = str(poi.get("type") or poi.get("poi_type") or "").strip()
    typecode = str(poi.get("typecode") or "").strip()

    for token in _DINNER_NAME_PATTERNS:
        if token in name:
            return MealSceneInference("dinner", (f"name:{token}",))
    for token in _LUNCH_NAME_PATTERNS:
        if token in name:
            return MealSceneInference("lunch", (f"name:{token}",))
    for token in _EITHER_NAME_PATTERNS:
        if token in name:
            return MealSceneInference("either", (f"name:{token}",))
    for token in _DINNER_TYPE_PATTERNS:
        if token in poi_type:
            return MealSceneInference("dinner", (f"type:{token}",))
    for token in _EITHER_TYPE_PATTERNS:
        if token in poi_type and re.search(r"街|市集|广场|城", name):
            evidence = [f"type:{token}", "name:street-or-market"]
            if typecode:
                evidence.append(f"typecode:{typecode}")
            return MealSceneInference("either", tuple(evidence))
    return MealSceneInference("none", ())


def semantic_category(poi: dict[str, Any], tags: Iterable[str] = ()) -> str:
    text = "|".join(
        [
            str(poi.get("name") or ""),
            str(poi.get("type") or poi.get("poi_type") or ""),
            *[str(tag) for tag in tags],
        ]
    ).lower()
    groups = (
        ("food_street", ("小吃", "美食", "夜市", "餐饮街", "food")),
        ("museum", ("博物馆", "纪念馆", "展览馆", "museum")),
        ("history", ("古迹", "遗址", "故居", "古城", "寺", "宫", "history")),
        ("nature", ("公园", "山", "湖", "湿地", "森林", "峡谷", "nature")),
        ("culture", ("艺术", "剧院", "文化", "书院", "culture")),
        ("commercial", ("步行街", "商业街", "商场", "commercial")),
    )
    for category, tokens in groups:
        if any(token in text for token in tokens):
            return category
    return "other"


def link_candidate_to_poi(
    proposal: CandidateAttraction,
    poi: dict[str, Any],
    *,
    cluster_id: int | None = None,
) -> OptimizerCandidate:
    """Merge LLM semantics with one server-owned POI, applying rule priority."""

    inferred = infer_meal_scene(poi)
    rule_confirmed = inferred.meal_scene != "none"
    scene = inferred.meal_scene if rule_confirmed else proposal.meal_scene
    source = "rule" if rule_confirmed else "llm"
    duration = proposal.duration_min
    if scene != "none" and duration == 120:
        # Candidate builder defaults to 120 for normal attractions; meal scenes
        # have an explicit product default of 90 minutes.
        duration = 90
    return OptimizerCandidate(
        **proposal.model_dump(exclude={"meal_scene", "duration_min"}),
        duration_min=duration,
        meal_scene=scene,
        poi_id=str(poi.get("id") or "") or None,
        rating=poi.get("rating"),
        open_time=poi.get("open_time"),
        location=poi["location"],
        poi_type=str(poi.get("type") or ""),
        typecode=str(poi.get("typecode") or ""),
        category=semantic_category(poi, proposal.semantic_tags),
        cluster_id=cluster_id,
        semantic_source=source,
        semantic_evidence=list(inferred.evidence) if rule_confirmed else ["llm:meal_scene"],
    )


def authoritative_poi_index(pois: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        str(poi.get("name") or "").strip(): poi
        for poi in pois
        if poi.get("location") and str(poi.get("name") or "").strip()
    }

"""Canonical PlanningBrief constraints and memory projections.

This module is deliberately model-free.  It validates the durable shape shared
by the brief UI, immutable Run snapshots and the planning graph.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any

from app.core.travel_memory import FACT_CATEGORIES, POLARITIES
from app.core.daily_counts import daily_bound_for_constraint


TRIP_CONSTRAINT_CATEGORIES = FACT_CATEGORIES - {"destination_history"}
LEGACY_CONSTRAINT_FIELDS = {
    "attraction_preference": "attraction_preference",
    "food_preference": "food_preference",
    "habit_preference": "travel_pace",
}

# A model occasionally labels an explicit negative instruction as ``prefer``.
# The text itself is authoritative for unambiguous, user-facing prohibitions.
_EXPLICIT_AVOID_TERMS = (
    "避开", "不去", "不要去", "不想去", "不安排", "不参观", "排除", "不吃", "忌口",
)


def _constraint_id(category: str, value: str, *, legacy_key: str = "") -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"tripagent-brief:{legacy_key}:{category}:{value}"))


def normalize_trip_constraint(
    item: dict[str, Any], *, default_source: str = "conversation"
) -> dict[str, Any]:
    category = str(item.get("category") or "").strip()
    if category not in TRIP_CONSTRAINT_CATEGORIES:
        raise ValueError("invalid trip constraint category")
    value = str(item.get("value_text") or item.get("value") or "").strip()
    if not value or len(value) > 500:
        raise ValueError("trip constraint value must contain 1-500 characters")
    polarity = str(item.get("polarity") or "fact")
    if polarity not in POLARITIES:
        raise ValueError("invalid trip constraint polarity")
    if any(term in value for term in _EXPLICIT_AVOID_TERMS):
        polarity = "avoid"
    if polarity == "require" and category in {"travel_pace", "schedule_preference"}:
        # Only remove a closed vocabulary of confirmation markers, then require
        # the entire remaining expression to parse. Unknown clauses stay intact.
        count_text = re.sub(r"\s*(?:（(?:硬性要求|硬约束|必须满足)）|\((?:硬性要求|硬约束|必须满足)\))\s*$", "", value)
        if daily_bound_for_constraint({"polarity": polarity, "value_text": count_text}) is not None:
            category, value = "travel_pace", count_text
    source = str(item.get("source") or default_source)
    if source not in {"conversation", "manual"}:
        source = default_source
    evidence = sorted(
        {int(value) for value in (item.get("evidence_sequences") or []) if int(value) > 0}
    )
    return {
        "id": str(item.get("id") or uuid.uuid4()),
        "category": category,
        "value_text": value,
        "polarity": polarity,
        "source": source,
        "evidence_sequences": evidence,
    }


def normalize_brief_data(data: dict[str, Any] | None) -> dict[str, Any]:
    result = dict(data or {})
    if "trip_budget" not in result and result.get("budget") is not None:
        result["trip_budget"] = result.get("budget")
    constraints: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in result.get("trip_constraints") or []:
        normalized = normalize_trip_constraint(dict(item))
        key = (
            normalized["category"],
            normalized["value_text"].casefold(),
            normalized["polarity"],
        )
        if key not in seen:
            seen.add(key)
            constraints.append(normalized)
    for field, category in LEGACY_CONSTRAINT_FIELDS.items():
        value = str(result.get(field) or "").strip()
        key = (category, value.casefold(), "prefer")
        if value and key not in seen:
            seen.add(key)
            constraints.append(
                {
                    "id": _constraint_id(category, value, legacy_key=field),
                    "category": category,
                    "value_text": value,
                    "polarity": "prefer",
                    "source": "conversation",
                    "evidence_sequences": [],
                }
            )
    focus = result.get("trip_focus")
    focus_constraint = {
        "sights_first": ("attraction_preference", "景点为主，餐饮仅作就近用餐"),
        "food_first": ("food_preference", "吃吃喝喝为主"),
        "balanced": ("other_travel_preference", "景点与餐饮均衡安排"),
    }.get(focus)
    if focus_constraint:
        category, value = focus_constraint
        key = (category, value.casefold(), "prefer")
        if key not in seen:
            constraints.append({
                "id": _constraint_id(category, value, legacy_key="trip_focus"),
                "category": category,
                "value_text": value,
                "polarity": "prefer",
                "source": "conversation",
                "evidence_sequences": [],
            })
    result["trip_constraints"] = constraints
    result["excluded_memory_fact_ids"] = sorted(
        {str(value) for value in result.get("excluded_memory_fact_ids") or [] if value}
    )
    return result


def matching_fingerprint(data: dict[str, Any], revision: int, facts: list[dict[str, Any]]) -> str:
    relevant = normalize_brief_data(data)
    payload = {
        "destination": relevant.get("destination"),
        "trip_budget": relevant.get("trip_budget"),
        "transport_budget": relevant.get("transport_budget"),
        "allowed_transport_modes": relevant.get("allowed_transport_modes"),
        "trip_constraints": relevant.get("trip_constraints", []),
        "excluded_memory_fact_ids": relevant.get("excluded_memory_fact_ids", []),
        "revision": int(revision),
        "fact_ids": [fact.get("id") for fact in facts if fact.get("status") == "active"],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _coverage(category: str, application_level: str, polarity: str = "prefer", value_text: str = "") -> dict[str, Any]:
    hard = polarity in {"require", "avoid"}
    if category == "transport_preference":
        from app.core.transport import constraint_modes
        if constraint_modes({"value_text": value_text, "polarity": polarity}):
            return {"status": "unverified", "stages": ["transport_check", "quality_gate"]}
    if hard and category not in {"attraction_preference", "travel_pace", "schedule_preference"}:
        return {
            "status": "unsupported",
            "stages": [],
            "reason_code": f"no_authoritative_{category}_validation",
        }

    if category == "attraction_preference":
        if hard:
            return {"status": "unverified", "stages": ["attraction_search", "candidate_builder", "optimizer", "quality_gate"]}
        return {"status": "advisory", "stages": ["candidate_builder"]}
    if category == "travel_pace":
        if hard:
            return {
                "status": "unverified",
                "stages": ["optimizer", "quality_gate"],
                "reason_code": "awaiting_optimizer_verification",
            }
        return {"status": "applied", "stages": ["optimizer"]}
    if category == "schedule_preference" and hard:
        return {"status": "unverified", "stages": ["attraction_search", "candidate_builder", "optimizer", "quality_gate"]}
    if application_level == "context_only" or category in {"destination_history", "accommodation_preference"}:
        return {"status": "advisory", "stages": ["finalize"] if category == "accommodation_preference" else []}
    stage_by_category = {
        "food_preference": ["attraction_search", "candidate_builder"],
        "budget_style": ["candidate_builder"],
        "companion_context": ["candidate_builder"],
        "transport_preference": ["candidate_builder"],
        "accessibility_need": ["candidate_builder"],
        "other_travel_preference": ["candidate_builder"],
    }
    stages = stage_by_category.get(category, ["candidate_builder"])
    return {"status": "advisory", "stages": stages}

def build_brief_projection(
    data: dict[str, Any],
    *,
    revision: int,
    frozen_facts: list[dict[str, Any]],
    projection: dict[str, Any] | None,
    match_status: str = "none",
    error_code: str | None = None,
) -> dict[str, Any]:
    normalized = normalize_brief_data(data)
    by_id = {
        str(fact.get("id")): fact
        for fact in frozen_facts
        if fact.get("status") == "active" and fact.get("id")
    }
    excluded = set(normalized["excluded_memory_fact_ids"])
    decisions = {
        str(item.get("fact_id")): item
        for item in (projection or {}).get("decisions", [])
        if item.get("decision") == "apply"
    }
    applied_facts: list[dict[str, Any]] = []
    excluded_facts: list[dict[str, Any]] = []
    effective = [dict(item) for item in normalized["trip_constraints"]]
    coverage: list[dict[str, Any]] = []
    for item in effective:
        detail = _coverage(item["category"], "hard" if item["polarity"] in {"require", "avoid"} else "preference", item["polarity"], item["value_text"])
        coverage.append({"constraint_id": item["id"], "source": item["source"], "category": item["category"], **detail})
    for fact_id, decision in decisions.items():
        fact = by_id.get(fact_id)
        if not fact:
            continue
        projected = {
            "fact_id": fact_id,
            "category": fact["category"],
            "value_text": fact["value_text"],
            "polarity": fact["polarity"],
            "scope_type": fact.get("scope_type", "global"),
            "scope_key": fact.get("scope_key") or {},
            "application_level": decision.get("application_level", "preference"),
            "reason_code": decision.get("reason_code", "supports_current_trip"),
            "source": "long_term_memory",
        }
        if fact_id in excluded:
            excluded_facts.append(projected)
            continue
        applied_facts.append(projected)
        constraint = {
            "id": f"memory:{fact_id}",
            "fact_id": fact_id,
            "category": fact["category"],
            "value_text": fact["value_text"],
            "polarity": fact["polarity"],
            "source": "long_term_memory",
            "application_level": projected["application_level"],
        }
        effective.append(constraint)
        detail = _coverage(fact["category"], projected["application_level"], fact["polarity"], fact["value_text"])
        coverage.append({"constraint_id": constraint["id"], "fact_id": fact_id, "source": "long_term_memory", "category": fact["category"], **detail})
    # Excluded facts remain explainable even if the latest model no longer selected them.
    for fact_id in sorted(excluded):
        if fact_id in decisions or fact_id not in by_id:
            continue
        fact = by_id[fact_id]
        excluded_facts.append({
            "fact_id": fact_id, "category": fact["category"],
            "value_text": fact["value_text"], "polarity": fact["polarity"],
            "scope_type": fact.get("scope_type", "global"),
            "scope_key": fact.get("scope_key") or {}, "source": "long_term_memory",
        })
    return {
        "data": normalized,
        "memory_context": {
            "revision": int(revision),
            "status": match_status,
            "error_code": error_code,
            "applied_facts": applied_facts,
            "excluded_facts": excluded_facts,
        },
        "effective_constraints": effective,
        "constraint_coverage": coverage,
    }


def compatibility_preferences(constraints: list[dict[str, Any]]) -> dict[str, str]:
    buckets = {"attraction_preference": [], "food_preference": [], "habit_preference": []}
    for item in constraints:
        value = constraint_directive(item)
        if not value:
            continue
        category = item.get("category")
        if category == "attraction_preference":
            buckets["attraction_preference"].append(value)
        elif category in {"food_preference", "dietary_requirement"}:
            buckets["food_preference"].append(value)
        elif category in {"travel_pace", "schedule_preference", "companion_context", "transport_preference", "accessibility_need"}:
            buckets["habit_preference"].append(value)
    return {key: "；".join(dict.fromkeys(values)) for key, values in buckets.items()}


def constraint_directive(item: dict[str, Any]) -> str:
    value = str(item.get("value_text") or "").strip()
    if not value:
        return ""
    prefix = {
        "prefer": "优先考虑",
        "avoid": "必须避开",
        "require": "必须满足",
        "fact": "仅作背景",
    }.get(str(item.get("polarity") or "prefer"), "仅作背景")
    if item.get("application_level") == "context_only":
        prefix = "仅作背景"
    return f"{prefix}：{value}"


def constraints_for_prompt(constraints: list[dict[str, Any]]) -> str:
    if not constraints:
        return "（本次没有额外旅行约束）"
    return "\n".join(
        f"- [{item.get('category')}/{item.get('polarity')}/{item.get('source')}] {constraint_directive(item)}"
        for item in constraints
    )


def planning_instruction(snapshot: dict[str, Any]) -> str:
    """Build the immutable, user-facing instruction consumed by formal planning.

    The conversation agent is responsible for collecting and confirming the
    brief.  Formal planning must consume that confirmed intent directly instead
    of asking another model to infer it from a reconstructed query.
    """
    destination = str(snapshot.get("destination") or "目的地未知").strip()
    start_date = str(snapshot.get("start_date") or "").strip()
    end_date = str(snapshot.get("end_date") or "").strip()
    days = snapshot.get("days")
    when = f"{start_date}至{end_date}" if start_date and end_date else "日期待确认"
    duration = f"{days}日" if days else "多日"
    parts = [f"为用户规划{when}的{destination}{duration}游"]
    for item in snapshot.get("effective_constraints") or []:
        directive = constraint_directive(item)
        if directive:
            parts.append(directive)
    budget = str(snapshot.get("trip_budget") or snapshot.get("budget") or "").strip()
    if budget:
        parts.append(f"本次预算：{budget}")
    return "；".join(dict.fromkeys(parts)) + "。"

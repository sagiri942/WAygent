"""Authoritative candidate-pool assembly around LLM travel semantics."""

from __future__ import annotations

import re
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from app.planning.helpers import cluster_pois_by_location
from app.planning.optimizer import candidate_pool_limit, daily_bound_for_constraint, parse_open_interval
from app.planning.schemas import CandidateAttraction, OptimizerCandidate
from app.planning.semantics import (
    authoritative_poi_index,
    is_featured_food_street,
    is_food_street,
    link_candidate_to_poi,
)


def _matching_names(text: str, names: Iterable[str]) -> list[str]:
    normalized = re.sub(r"\s+", "", text)
    names = [name for name in names if name]
    tokens = {re.sub(r"\s+", "", name) for name in names}
    if not tokens:
        return []
    # At each occurrence a complete, longer POI name owns its shorter prefix.
    # Separate mentions still bind both (e.g. 公园、以及公园-阁).
    pattern = "|".join(re.escape(token) for token in sorted(tokens, key=lambda token: (-len(token), token)))
    found = {match.group() for match in re.finditer(pattern, normalized)}
    return [name for name in names if re.sub(r"\s+", "", name) in found]


def excluded_poi_names(
    pois: Sequence[dict[str, Any]], constraints: Sequence[dict[str, Any]]
) -> set[str]:
    names = [str(poi.get("name") or "") for poi in pois]
    excluded: set[str] = set()
    for item in constraints:
        if item.get("polarity") != "avoid":
            continue
        excluded.update(_matching_names(str(item.get("value_text") or ""), names))
    return excluded


@dataclass(frozen=True)
class ParsedConstraint:
    """One expression's named field bindings, residual text and conflicts."""
    item: dict[str, Any]
    flags_by_name: dict[str, dict[str, Any]]
    residual: str
    opening_requested: bool
    conflicting_fields: tuple[str, ...]


def _empty_flags() -> dict[str, Any]:
    return dict(must_visit=False, fixed_day=None, fixed_start_min=None,
                must_be_first=False, must_be_last=False, before_poi_names=[],
                evidence_constraint_ids=[])


def _binary_precedence(text: str, names: Sequence[str]) -> tuple[str, str] | None:
    """Bind complete supported expressions by grammar role, never text order."""
    compact = re.sub(r"\s+", "", text)
    by_token = {re.sub(r"\s+", "", name): name for name in names}
    if (len(names) != 2 or len(by_token) != 2
            or len(re.findall(r"之前|先于", compact)) != 1
            or any(compact.count(token) != 1 for token in by_token)):
        return None
    endpoint = "(?:" + "|".join(re.escape(token) for token in by_token) + ")"
    # A [must] precedes B; A [must] before B. Prefix/suffix permutations and
    # additional clauses are unsupported instead of partially consumed.
    for connector, suffix in (("先于", ""), ("在", "之前")):
        relation = re.fullmatch(
            rf"(?P<before>{endpoint})(?:必须)?{connector}(?P<after>{endpoint}){suffix}[。.!！]?",
            compact,
        )
        if relation and relation["before"] != relation["after"]:
            return by_token[relation["before"]], by_token[relation["after"]]
    return None


def _parse_constraint(item: dict[str, Any], all_names: Sequence[str]) -> ParsedConstraint:
    text = str(item.get("value_text") or "")
    matched = _matching_names(text, all_names)
    flags_by_name = {name: _empty_flags() for name in matched}
    constraint_id = str(item.get("id") or item.get("fact_id") or "").strip()
    if re.search(r"之前|先于", text):
        relation = _binary_precedence(text, matched)
        for flags in flags_by_name.values():
            if constraint_id:
                flags["evidence_constraint_ids"] = [constraint_id]
            flags["must_visit"] = relation is not None and item.get("polarity") == "require"
        if relation is not None:
            predecessor, successor = relation
            flags_by_name[predecessor]["before_poi_names"] = [successor]
        # Relation entries use full consumption. Generic token stripping below
        # must never erase unsupported ordering syntax or extra requirements.
        residual = "" if relation is not None else re.sub(r"[\W_]+", "", text.casefold())
        return ParsedConstraint(item, flags_by_name, residual, False, ())
    days = {int(match.group(1) or match.group(2)) for match in
            re.finditer(r"(?:第\s*(\d+)\s*天|Day\s*(\d+))", text, re.IGNORECASE)}
    times = {int(match.group(1)) * 60 + int(match.group(2)) for match in
             re.finditer(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)", text)}
    fixed_time_requested = any(token in text for token in ("预约", "固定", "准时", "开始"))
    opening = any(token in text for token in ("开放", "闭馆", "营业时间"))
    conflicts = []
    if len(days) > 1:
        conflicts.append("fixed_day")
    if fixed_time_requested and len(times) > 1:
        conflicts.append("fixed_start_min")
    for name, flags in flags_by_name.items():
        if constraint_id:
            flags["evidence_constraint_ids"] = [constraint_id]
        hard = item.get("polarity") == "require"
        if len(days) == 1:
            flags["fixed_day"] = next(iter(days))
        if fixed_time_requested and len(times) == 1:
            flags["fixed_start_min"] = next(iter(times))
        flags["must_be_first"] = any(token in text for token in ("首站", "第一站", "最先"))
        flags["must_be_last"] = any(token in text for token in ("末站", "最后一站", "最后去"))
        flags["must_visit"] = hard and (
            any(token in text for token in ("必去", "必须", "一定要", "不能删"))
            or item.get("category") == "schedule_preference"
            or bool(days or (fixed_time_requested and times) or opening)
            or flags["must_be_first"] or flags["must_be_last"]
        )

    residual = text.casefold()
    for name in sorted(matched, key=len, reverse=True):
        residual = residual.replace(name.casefold(), "")
    # Consume only tokens with an unambiguous field binding in this entry.
    if len(days) == 1:
        residual = re.sub(r"(?:第\s*\d+\s*天|day\s*\d+)", "", residual)
    phrases = ["在", "且", "并且", "and"]
    if len(days) == 1 and matched:
        phrases.append("去")
    if fixed_time_requested and len(times) == 1:
        residual = re.sub(r"(?<!\d)(?:[01]?\d|2[0-3]):[0-5]\d", "", residual)
        phrases.extend(("固定", "预约", "准时", "开始"))
    if any(flag["must_be_first"] for flag in flags_by_name.values()):
        phrases.extend(("第一站", "首站", "最先"))
    if any(flag["must_be_last"] for flag in flags_by_name.values()):
        phrases.extend(("最后一站", "末站", "最后去"))
    if any(flag["must_visit"] for flag in flags_by_name.values()):
        phrases.extend(("必须去", "一定要去", "必须", "一定要", "必去", "不能删"))
    if item.get("polarity") == "avoid":
        phrases.extend(("不要去", "避开", "不想去", "不去"))
    if opening:
        phrases.extend(("营业时间内", "开放时间内", "营业时间", "开放时间", "开放", "闭馆", "时间内"))
    for phrase in sorted(phrases, key=len, reverse=True):
        residual = residual.replace(phrase.casefold(), "")
    residual = re.sub(r"[\W_]+", "", residual)
    return ParsedConstraint(item, flags_by_name, residual, opening, tuple(conflicts))


def _has_schedule_terms(binding: ParsedConstraint) -> bool:
    text = str(binding.item.get("value_text") or "")
    return (
        bool(binding.conflicting_fields)
        or binding.opening_requested
        or any(
            flag[field] is not None and bool(flag[field])
            for flag in binding.flags_by_name.values()
            for field in ("fixed_day", "fixed_start_min", "must_be_first", "must_be_last", "before_poi_names")
        )
        or re.search(r"第\s*\d+\s*天|Day\s*\d+", text, re.IGNORECASE) is not None
        or any(marker in text for marker in ("预约", "固定", "准时", "开始", "先于", "之前"))
    )


def _constraint_flags(poi_name: str, parsed: Sequence[ParsedConstraint]) -> dict[str, Any]:
    flags = _empty_flags()
    for binding in parsed:
        source = binding.flags_by_name.get(poi_name)
        if source is None:
            continue
        for field in ("fixed_day", "fixed_start_min"):
            if flags[field] is None and source[field] is not None:
                flags[field] = source[field]
        for field in ("must_visit", "must_be_first", "must_be_last"):
            flags[field] |= source[field]
        for field in ("before_poi_names", "evidence_constraint_ids"):
            flags[field] = list(dict.fromkeys(flags[field] + source[field]))
    return flags


def _conflicting_schedule_names(parsed: Sequence[ParsedConstraint]) -> set[str]:
    values: dict[tuple[str, str], set[int]] = {}
    conflicts: set[str] = set()
    for binding in parsed:
        if binding.item.get("polarity") != "require":
            continue
        if binding.conflicting_fields:
            conflicts.update(binding.flags_by_name)
        for name, flags in binding.flags_by_name.items():
            for field in ("fixed_day", "fixed_start_min"):
                if flags[field] is not None:
                    values.setdefault((name, field), set()).add(flags[field])
    conflicts.update(name for (name, _field), choices in values.items() if len(choices) > 1)
    return conflicts


def _default_proposal(poi: dict[str, Any], preference: str | None) -> CandidateAttraction:
    name = str(poi.get("name") or "")
    pref_tokens = [token for token in re.split(r"[、,，；;\s]+", preference or "") if len(token) >= 2]
    preference_match = 0.7 if any(token in name or token in str(poi.get("type") or "") for token in pref_tokens) else 0.5
    rating = float(poi.get("rating") or 0)
    return CandidateAttraction(
        poi_name=name,
        duration_min=120,
        preference_match=preference_match,
        representativeness=max(0.3, min(1.0, rating / 5 if rating else 0.5)),
        preferred_period="any",
        meal_scene="none",
    )


def build_authoritative_candidates(
    proposals: Sequence[CandidateAttraction | dict[str, Any]],
    pois: Sequence[dict[str, Any]],
    *,
    days: int,
    constraints: Sequence[dict[str, Any]] = (),
    attraction_preference: str | None = None,
    food_focused: bool = False,
) -> tuple[list[OptimizerCandidate], list[str]]:
    """Drop hallucinations, fill the target pool, and enforce server semantics."""

    index = authoritative_poi_index(pois)
    excluded = excluded_poi_names(pois, constraints)
    eligible = [poi for name, poi in index.items() if name not in excluded]
    cluster_map = cluster_pois_by_location(eligible, max(1, days))
    warnings: list[str] = []
    linked: dict[str, OptimizerCandidate] = {}
    for raw in proposals:
        try:
            proposal = raw if isinstance(raw, CandidateAttraction) else CandidateAttraction.model_validate(raw)
        except Exception as exc:
            warnings.append(f"INVALID_LLM_CANDIDATE:{type(exc).__name__}")
            continue
        poi = index.get(proposal.poi_name)
        if poi is None or proposal.poi_name in excluded:
            warnings.append(f"UNKNOWN_OR_EXCLUDED_POI:{proposal.poi_name}")
            continue
        linked[proposal.poi_name] = link_candidate_to_poi(
            proposal, poi, cluster_id=cluster_map.get(proposal.poi_name)
        )

    # Food streets are meal destinations, not default core attractions.  In a
    # sightseeing-led trip retain at most one well-rated local landmark; a
    # user may still explicitly require a named street or choose food focus.
    required_names = {
        name
        for item in constraints
        if item.get("polarity") == "require"
        for name in _matching_names(str(item.get("value_text") or ""), index)
    }
    allowed_food = set(required_names)
    if not food_focused:
        featured = [
            candidate for candidate in linked.values()
            if is_featured_food_street(index[candidate.poi_name])
        ]
        if featured:
            allowed_food.add(max(featured, key=_candidate_rank).poi_name)
        elif not allowed_food:
            featured_pois = [poi for poi in eligible if is_featured_food_street(poi)]
            if featured_pois:
                allowed_food.add(str(max(
                    featured_pois,
                    key=lambda poi: float(poi.get("rating") or 0),
                ).get("name") or ""))
        for name in list(linked):
            if is_food_street(index[name]) and name not in allowed_food:
                linked.pop(name)
                warnings.append(f"FOOD_STREET_DEPRIORITIZED:{name}")

    desired = min(candidate_pool_limit(days), len(eligible))
    filler_eligible = eligible if food_focused else [
        poi for poi in eligible
        if not is_food_street(poi) or str(poi.get("name") or "") in allowed_food
    ]
    fillers = sorted(
        filler_eligible,
        key=lambda poi: (-float(poi.get("rating") or 0), str(poi.get("name") or "")),
    )
    for poi in fillers:
        name = str(poi.get("name") or "")
        if len(linked) >= desired:
            break
        if name not in linked:
            proposal = _default_proposal(poi, attraction_preference)
            linked[name] = link_candidate_to_poi(proposal, poi, cluster_id=cluster_map.get(name))
            warnings.append(f"SERVER_FILLED_CANDIDATE:{name}")

    parsed = [_parse_constraint(item, list(index)) for item in constraints]
    for name, poi in index.items():
        flags = _constraint_flags(name, parsed)
        if not flags["must_visit"]:
            continue
        if name in excluded:
            warnings.append(f"CONFLICTING_MUST_AND_AVOID:{name}")
            continue
        if name not in linked:
            proposal = _default_proposal(poi, attraction_preference)
            linked[name] = link_candidate_to_poi(proposal, poi, cluster_id=cluster_map.get(name))

    result: list[OptimizerCandidate] = []
    for name, candidate in linked.items():
        flags = _constraint_flags(name, parsed)
        merged_evidence = list(dict.fromkeys(candidate.evidence_constraint_ids + flags.pop("evidence_constraint_ids")))
        result.append(candidate.model_copy(update={**flags, "evidence_constraint_ids": merged_evidence}))
    result.sort(key=lambda item: (not item.must_visit, -_candidate_rank(item), item.poi_name))
    return result, warnings


def resolve_constraint_coverage(
    constraints: Sequence[dict[str, Any]],
    coverage: Sequence[dict[str, Any]],
    pois: Sequence[dict[str, Any]],
    candidates: Sequence[OptimizerCandidate],
    days: int | None = None,
) -> list[dict[str, Any]]:
    """Resolve hard-constraint coverage only after authoritative candidates exist."""
    by_id = {
        str(item.get("id") or item.get("fact_id") or ""): item
        for item in constraints
    }
    poi_names = [str(poi.get("name") or "") for poi in pois]
    candidate_by_name = {candidate.poi_name: candidate for candidate in candidates}
    avoided_names = {
        name
        for item in constraints
        if item.get("polarity") == "avoid"
        for name in _matching_names(str(item.get("value_text") or ""), poi_names)
    }
    parsed_constraints = [_parse_constraint(item, poi_names) for item in constraints]
    parsed_by_id = {str(binding.item.get("id") or binding.item.get("fact_id") or ""): binding for binding in parsed_constraints}
    conflicting_names = _conflicting_schedule_names(parsed_constraints)
    updated: list[dict[str, Any]] = []
    for original in coverage:
        entry = dict(original)
        constraint = by_id.get(str(entry.get("constraint_id") or ""))
        if not constraint or constraint.get("polarity") not in {"require", "avoid"}:
            updated.append(entry)
            continue
        category = str(constraint.get("category") or "")
        value = str(constraint.get("value_text") or "")
        binding = parsed_by_id[str(entry.get("constraint_id") or "")]
        embedded_schedule = category == "attraction_preference" and _has_schedule_terms(binding)
        if category == "attraction_preference" and not embedded_schedule:
            matched = _matching_names(value, poi_names)
            normalized_value = value.replace(" ", "")
            explicit_matches = [name for name in matched if name.replace(" ", "") in normalized_value]
            query = value
            for marker in ("必须去", "必去", "一定要去", "去", "避开", "不要去"):
                query = query.replace(marker, "")
            query = re.sub(r"[\W_]+", "", query)
            partial_matches = [
                name for name in poi_names
                if query and query in re.sub(r"[\W_]+", "", name)
            ]
            entry["stages"] = ["attraction_search", "candidate_builder", "optimizer", "quality_gate"]
            if not matched and len(partial_matches) > 1:
                entry.update(status="unsupported", reason_code="ambiguous_authoritative_poi_match")
            elif not matched:
                entry.update(status="unsupported", reason_code="authoritative_poi_not_found")
            elif constraint.get("polarity") == "require" and set(matched) & avoided_names:
                entry.update(status="unsupported", reason_code="required_poi_also_avoided")
            elif len(explicit_matches) != len(matched):
                entry.update(status="unsupported", reason_code="ambiguous_authoritative_poi_match")
            elif binding.residual:
                entry.update(status="unsupported", reason_code="attraction_constraint_not_verifiable")
            elif constraint.get("polarity") == "avoid":
                if any(name in candidate_by_name for name in matched):
                    entry.update(status="unsupported", reason_code="avoided_poi_retained")
                else:
                    entry.update(status="applied")
                    entry.pop("reason_code", None)
            else:
                if all(name in candidate_by_name and candidate_by_name[name].must_visit for name in matched):
                    entry.update(status="applied")
                    entry.pop("reason_code", None)
                else:
                    entry.update(status="unsupported", reason_code="required_poi_not_enforced")
        elif category == "travel_pace":
            if daily_bound_for_constraint(constraint) is not None:
                entry.update(status="applied", stages=["optimizer", "quality_gate"])
                entry.pop("reason_code", None)
            else:
                entry.update(status="unsupported", reason_code="unsupported_daily_count_expression", stages=[])
        elif category == "schedule_preference" or embedded_schedule:
            matched = _matching_names(value, poi_names)
            flags = list(binding.flags_by_name.values())
            schedule_flags = ("fixed_day", "fixed_start_min", "must_be_first", "must_be_last", "before_poi_names")
            has_opening_request = binding.opening_requested
            has_opening_data = bool(matched) and all(
                name in candidate_by_name and parse_open_interval(candidate_by_name[name].open_time, require_complete=True) is not None
                for name in matched
            )
            fixed_flags = ("fixed_day", "fixed_start_min", "must_be_first", "must_be_last")
            has_relation = any(flag.get("before_poi_names") for flag in flags)
            one_target_is_parsed = len(matched) == 1 and any(any(flag.get(key) for key in schedule_flags) for flag in flags)
            relation_is_parsed = len(matched) > 1 and has_relation and not any(any(flag.get(key) for key in fixed_flags) for flag in flags)
            opening_is_parsed = has_opening_request and len(matched) == 1 and has_opening_data
            parsed = (
                (one_target_is_parsed or relation_is_parsed or opening_is_parsed)
                and not binding.residual
                and all(
                    name in candidate_by_name and all(
                        getattr(candidate_by_name[name], field) == flag[field]
                        for field in ("fixed_day", "fixed_start_min") if flag[field] is not None
                    ) and all(
                        not flag[field] or getattr(candidate_by_name[name], field)
                        for field in ("must_visit", "must_be_first", "must_be_last")
                    ) and set(flag["before_poi_names"]).issubset(candidate_by_name[name].before_poi_names)
                    for name, flag in binding.flags_by_name.items()
                )
            )
            out_of_range_day = any(
                flag.get("fixed_day") is not None and days is not None and not 1 <= flag["fixed_day"] <= days
                for flag in flags
            )
            fixed_outside_opening = False
            if has_opening_request and has_opening_data:
                for name, flag in binding.flags_by_name.items():
                    candidate = candidate_by_name[name]
                    opening = parse_open_interval(candidate.open_time, require_complete=True)
                    start = candidate.fixed_start_min
                    if opening is not None and start is not None and not (
                        opening[0] <= start and start + candidate.duration_min <= opening[1]
                    ):
                        fixed_outside_opening = True
            entry["stages"] = ["attraction_search", "candidate_builder", "optimizer", "quality_gate"]
            if constraint.get("polarity") != "require":
                entry.update(status="unsupported", reason_code="schedule_constraint_not_verifiable")
            elif set(matched) & conflicting_names:
                entry.update(status="unsupported", reason_code="schedule_constraint_conflict")
            elif fixed_outside_opening:
                entry.update(status="unsupported", reason_code="schedule_time_outside_opening")
            elif out_of_range_day:
                entry.update(status="unsupported", reason_code="scheduled_day_out_of_range")
            elif not matched or not parsed or (has_opening_request and not has_opening_data):
                entry.update(status="unsupported", reason_code="schedule_constraint_not_verifiable")
            else:
                entry.update(status="applied")
                entry.pop("reason_code", None)
        updated.append(entry)
    return updated

def _candidate_rank(candidate: OptimizerCandidate) -> float:
    return (
        float(candidate.rating or 0) / 5
        + candidate.preference_match * 1.5
        + candidate.representativeness * 1.2
    )


def candidate_pool_fingerprint(candidates: Sequence[OptimizerCandidate | dict[str, Any]]) -> str:
    payload = [
        item.model_dump(mode="json") if isinstance(item, OptimizerCandidate) else item
        for item in candidates
    ]
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

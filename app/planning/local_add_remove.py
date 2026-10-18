"""Explicit bounded same-day attraction set changes."""
from __future__ import annotations

import copy
import re
from typing import Any, Mapping

from app.planning.local_reorder import LocalReorderRejected, LocalReorderResult, extract_explicit_target_day, reorder_single_day

_OPERATION = re.compile(r"增加|新增|添加|加入|删除|移除|去掉")


def _require_named_poi(raw, name):
    from app.planning.poi_resolution import named_poi_choices, unique_named_poi
    poi = unique_named_poi(raw, name)
    if poi is not None:
        return poi
    choices = named_poi_choices(raw, name)
    suggestion = "可选全名：" + "、".join(choices) + "。" if choices else ""
    raise LocalReorderRejected(
        "revision_authoritative_poi_unresolved",
        "没有找到唯一名称完全匹配的权威地点，请确认景点全名。" + suggestion + "原版本保持不变。",
        data={"candidates": choices},
    )


def is_add_remove_request(notes: str) -> bool:
    return bool(_OPERATION.search(str(notes or "")))


def parse_day_set_change(notes: str) -> tuple[int, dict[str, str]]:
    text = str(notes or "").strip()
    matches = list(_OPERATION.finditer(text))
    if len(matches) != 1 or re.search(r"替换|换成|改日期|改天数|改目的地|不要|不必|别|是否|要不要|能否|如果|或者|还是", text):
        raise LocalReorderRejected("revision_intent_unresolved", "请明确一个目标日和一个要增加或删除的景点；原版本保持不变。")
    match = matches[0]
    # Consume the entire supported expression, including optional order-only wording.
    prefix = text[:match.start()].strip(" ，,：:")
    day = extract_explicit_target_day(prefix)
    prefix = re.sub(r"(?:第\s*)?([0-9]+|[一二两三四五六七八九十]{1,3})\s*天", "", prefix)
    prefix = re.sub(r"请|帮我|把|在|的|景点|重排|顺序|重新安排", "", prefix).strip(" ，,：:")
    name = text[match.end():].strip(" ，,。.!！：:\t\n")
    name = re.sub(r"^(?:景点|一个景点|一个)\s*", "", name).strip()
    if prefix or not name or re.search(r"[，,；;、]|和|以及|然后|再去|先去|预约|必须|\d{1,2}[:：]\d{2}", name):
        raise LocalReorderRejected("revision_intent_unresolved", "无法完整确认这条增删请求，请只指定一个景点；原版本保持不变。")
    return day, {"kind": "remove" if match.group() in {"删除", "移除", "去掉"} else "add", "poi_name": name}


def _merge_hard_rules(original: Mapping[str, Any], policy: Mapping[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(dict(original))
    for key in ("fixed_day", "fixed_start_min", "must_be_first", "must_be_last", "must_visit"):
        value = policy.get(key)
        if value is None or value is False:
            continue
        existing = merged.get(key)
        if existing is not None and existing is not False and existing != value:
            raise LocalReorderRejected("local_set_conflict", "地点冻结要求互相冲突，原版本保持不变。")
        merged[key] = value
    merged["before_poi_names"] = list(dict.fromkeys([
        *(merged.get("before_poi_names") or []), *(policy.get("before_poi_names") or []),
    ]))
    return merged


def _release_local_selection(constraints, pool, pois, *, name, day, days):
    """A subsequent explicit edit supersedes that stop's prior local selection.

    Original brief requirements, appointments and precedence rules remain hard.
    """
    retained = [item for item in constraints if not (
        item.get("source") in {"explicit_local_revision", "explicit_manual_revision"}
        and item.get("polarity") == "require"
        and (item.get("category"), item.get("value_text")) in {
            ("attraction_preference", f"必须去{name}"),
            ("schedule_preference", f"第{day}天去{name}"),
        }
    )]
    if len(retained) == len(constraints):
        return constraints, pool
    from app.planning.candidate_builder import build_authoritative_candidates
    from app.planning.schemas import CandidateAttraction
    original = next((item for item in pool if item.get("poi_name") == name), {})
    proposal = {key: value for key, value in original.items() if key in CandidateAttraction.model_fields}
    rebuilt, _ = build_authoritative_candidates([proposal], pois, days=days, constraints=retained)
    policy = next((item.model_dump(mode="json") for item in rebuilt if item.poi_name == name), {})
    defaults = dict(must_visit=False, fixed_day=None)
    updated = {**original, **{field: policy.get(field, default) for field, default in defaults.items()}}
    return retained, [updated if item.get("poi_name") == name else item for item in pool]


def _refresh_changed_day_theme(result: LocalReorderResult, day: int) -> None:
    """Derive the changed day's label from verified stops, without a model."""
    target = next(item for item in result.plan["days"] if int(item["day"]) == day)
    theme = "、".join(item["name"] for item in target["timeline"] if item.get("type") == "attraction")
    target["theme"] = theme
    route = next(item for item in result.planner_state["route"] if int(item["day"]) == day)
    route["theme"] = theme


async def change_single_day_set(
    plan: Mapping[str, Any], planner_state: Mapping[str, Any], *, target_day: int,
    modification_notes: str, provider, poi_provider=None,
) -> LocalReorderResult:
    """Change exactly one named stop, preserving every other date and hard rule."""
    from app.planning.candidate_builder import build_authoritative_candidates, candidate_pool_fingerprint, excluded_poi_names, resolve_constraint_coverage
    from app.planning.optimizer import daily_bounds_from_constraints
    from app.providers.amap.poi import search_attraction_pois_async
    import asyncio
    import os

    day, operation = parse_day_set_change(modification_notes)
    if day != target_day:
        raise LocalReorderRejected("revision_day_mismatch", "目标日期不一致，原版本保持不变。")
    changed_plan, changed_state = copy.deepcopy(dict(plan)), copy.deepcopy(dict(planner_state))
    target = next((item for item in changed_plan.get("days") or [] if int(item.get("day") or 0) == day), None)
    route = next((item for item in changed_state.get("route") or [] if int(item.get("day") or 0) == day), None)
    if target is None or route is None:
        raise LocalReorderRejected("revision_day_invalid", "找不到目标日，原版本保持不变。")
    name = operation["poi_name"]
    names = [item.get("name") for item in route.get("spots") or []]
    plan_names = [item.get("name") for item in target.get("timeline") or [] if item.get("type") == "attraction"]
    if not names or len(names) != len(set(names)) or set(names) != set(plan_names) or len(plan_names) != len(names):
        raise LocalReorderRejected("local_reorder_unverified", "基准日缺少一致的已保存景点，原版本保持不变。")
    constraints = copy.deepcopy(changed_state.get("effective_constraints") or changed_plan.get("effective_constraints") or [])
    pool = changed_state.get("candidate_pool") or []
    pois = changed_state.get("pois") or []
    if operation["kind"] == "remove":
        constraints, pool = _release_local_selection(
            constraints, pool, pois, name=name, day=day,
            days=int(changed_state.get("days") or len(changed_plan.get("days") or [])),
        )
    semantic = next((item for item in pool if item.get("poi_name") == name), None)
    poi = next((item for item in pois if item.get("name") == name), None)
    if operation["kind"] == "remove":
        if name not in names:
            raise LocalReorderRejected("revision_poi_unresolved", "要删除的景点不在目标日，原版本保持不变。")
        if semantic and any(semantic.get(key) for key in ("must_visit", "fixed_day", "fixed_start_min", "must_be_first", "must_be_last")):
            raise LocalReorderRejected("local_set_conflict", "删除与已冻结的必去、指定日或预约要求冲突，请先确认是否修改硬约束；原版本保持不变。")
        if any(name in (item.get("before_poi_names") or []) or (item.get("poi_name") == name and item.get("before_poi_names")) for item in pool):
            raise LocalReorderRejected("local_set_conflict", "删除与已冻结的景点先后要求冲突，原版本保持不变。")
        required_candidates, _ = build_authoritative_candidates([], pois, days=int(changed_state.get("days") or len(changed_plan.get("days") or [])), constraints=constraints)
        if any(item.poi_name == name and item.must_visit for item in required_candidates):
            raise LocalReorderRejected("local_set_conflict", "删除与已冻结的必去要求冲突，原版本保持不变。")
        target["timeline"] = [item for item in target.get("timeline") or [] if not (item.get("type") == "attraction" and item.get("name") == name)]
        route["spots"] = [item for item in route["spots"] if item.get("name") != name]
        pool = [item for item in pool if item.get("poi_name") != name]
        polarity, value = "avoid", f"不要去{name}"
    else:
        other_names = {
            item.get("name") for other in changed_plan.get("days") or [] if int(other.get("day") or 0) != day
            for item in other.get("timeline") or [] if item.get("type") == "attraction"
        }
        if name in other_names:
            raise LocalReorderRejected("local_set_cross_day_duplicate", "新增地点已安排在其他日期，原版本保持不变，请确认是否扩大修改范围。")
        if name in names:
            raise LocalReorderRejected("local_set_no_change", "新增地点已在当天行程中，原版本保持不变。")
        if not semantic or not poi:
            try:
                raw = await asyncio.wait_for((poi_provider or search_attraction_pois_async)(
                    str(changed_plan.get("destination") or changed_state.get("destination") or ""),
                    os.getenv("AMAP_API_KEY", ""), keywords=name, types="",
                ), timeout=10)
            except Exception as exc:
                raise LocalReorderRejected("revision_authoritative_poi_unavailable", "权威地点服务暂时无法确认指定新增景点，原版本保持不变。") from exc
            poi = _require_named_poi(raw, name)
            pois = [item for item in pois if item.get("name") != name] + [poi]
        if name in excluded_poi_names(pois, constraints):
            raise LocalReorderRejected("local_set_conflict", "新增地点与已冻结的避开要求冲突，原版本保持不变。")
        # Reject identity aliases of an attraction already booked on another day.
        other_ids = {item.get("id") or item.get("poi_id") for other in changed_plan.get("days") or [] if int(other.get("day") or 0) != day for item in other.get("timeline") or [] if item.get("type") == "attraction"}
        other_ids.update(item.get("id") for item in pois if item.get("name") in other_names)
        if poi.get("id") and poi["id"] in other_ids:
            raise LocalReorderRejected("local_set_cross_day_duplicate", "权威地点已安排在其他日期，原版本保持不变。")
        explicit_require = {"id": f"local-add-day-{day}-{name}", "category": "attraction_preference", "polarity": "require", "value_text": f"必须去{name}"}
        built, _ = build_authoritative_candidates([], pois, days=int(changed_state.get("days") or len(changed_plan.get("days") or [])), constraints=[*constraints, explicit_require])
        relevant = [item for item in constraints if item.get("polarity") in {"require", "avoid"} and name in str(item.get("value_text") or "")]
        coverage = resolve_constraint_coverage(constraints, [
            {"constraint_id": item.get("id") or item.get("fact_id"), "status": "unverified"}
            for item in relevant
        ], pois, built, days=int(changed_state.get("days") or len(changed_plan.get("days") or [])))
        if any(item.get("status") != "applied" for item in coverage):
            raise LocalReorderRejected("local_set_conflict", "指定景点的冻结预约或硬约束无法完整核验，原版本保持不变。", data={"constraint_failures": coverage})
        policy = next((item.model_dump(mode="json") for item in built if item.poi_name == name), None)
        if policy is None:
            raise LocalReorderRejected("revision_poi_unresolved", "指定地点无法进入可核验候选，原版本保持不变。")
        semantic = {**policy, **(semantic or {}), "poi_name": name, "must_visit": True}
        semantic = _merge_hard_rules(semantic, policy)
        policies = {item.poi_name: item.model_dump(mode="json") for item in built}
        target_names = {*names, name}
        if any(
            (item.poi_name == name and any(successor not in target_names for successor in item.before_poi_names))
            or (item.poi_name not in target_names and name in item.before_poi_names)
            for item in built
        ):
            raise LocalReorderRejected("local_set_cross_day_constraint", "新增地点涉及其他日期的先后要求，请先确认扩大修改范围；原版本保持不变。")
        pool = [
            _merge_hard_rules(item, policies.get(str(item.get("poi_name") or ""), {}))
            if item.get("poi_name") in target_names else item
            for item in pool
        ]
        if semantic.get("fixed_day") not in (None, day):
            raise LocalReorderRejected("local_set_conflict", "新增与已冻结的指定日要求冲突，原版本保持不变。")
        semantic["fixed_day"] = day
        pool = [item for item in pool if item.get("poi_name") != name] + [semantic]
        target["timeline"].append({**copy.deepcopy(poi), "type": "attraction"})
        route["spots"].append({"name": name})
        polarity, value = "require", f"必须去{name}"
    minimum, maximum = daily_bounds_from_constraints(constraints)
    count = len(route["spots"])
    if count < max(1, minimum or 0) or (maximum is not None and count > maximum):
        raise LocalReorderRejected("local_set_daily_count_conflict", "修改后的景点数量与每日数量要求冲突，原版本保持不变，请确认是否调整要求。")
    constraint = {"id": f"local-{operation['kind']}-day-{day}-{name}", "category": "attraction_preference", "polarity": polarity, "value_text": value, "source": "explicit_local_revision"}
    constraints.append(constraint)
    if operation["kind"] == "add":
        constraints.append({"id": f"local-add-fixed-day-{day}-{name}", "category": "schedule_preference", "polarity": "require", "value_text": f"第{day}天去{name}", "source": "explicit_local_revision"})
    changed_state["candidate_pool"] = pool
    changed_state["pois"] = pois
    changed_state["effective_constraints"] = constraints
    changed_plan["effective_constraints"] = copy.deepcopy(constraints)
    additions = [constraint]
    if operation["kind"] == "add":
        additions.append(constraints[-1])
    changed_state["constraint_coverage"] = [*(changed_state.get("constraint_coverage") or []), *[
        {"constraint_id": item["id"], "status": "applied", "stages": ["candidate_builder", "local_set_solver", "time_check"]}
        for item in additions
    ]]
    result = await reorder_single_day(changed_plan, changed_state, target_day=day, modification_notes=f"重排第{day}天", provider=provider)
    scope = {"kind": "local_add_remove", "day": day, "operation": operation}
    result.plan["revision_scope"] = scope
    result.planner_state["revision_scope"] = scope
    result.planner_state["modification_notes"] = modification_notes
    result.planner_state["candidate_pool_fingerprint"] = candidate_pool_fingerprint(pool)
    result.plan["constraint_coverage"] = copy.deepcopy(result.planner_state["constraint_coverage"])
    result.plan["quality_report"]["validation_scope"] = scope
    result.planner_state["quality_report"]["validation_scope"] = scope
    result.verification["operation"] = operation
    _refresh_changed_day_theme(result, day)
    return result


_REPLACEMENT_VERB = re.compile(r"替换成|替换为|换成|换为|改成|改为")


def is_replace_request(notes: str) -> bool:
    """Recognize replacement intent before the broader add/remove dispatcher."""
    text = str(notes or "")
    return bool(_REPLACEMENT_VERB.search(text) or re.search(r"用.+替换", text))


def parse_day_replacement(notes: str) -> tuple[int, dict[str, str]]:
    text = str(notes or "").strip()
    if not is_replace_request(text) or re.search(r"不要|不必|别|是否|要不要|能否|如果|或者|还是|改日期|改天数|改目的地", text):
        raise LocalReorderRejected("revision_intent_unresolved", "请明确一个目标日、原景点和新景点；原版本保持不变。")
    if re.search(r"增加|新增|添加|加入|删除|移除|去掉|并|同时|另外|顺便", text):
        raise LocalReorderRejected("revision_intent_unresolved", "一次只能明确替换一个景点；复合操作需要拆分确认，原版本保持不变。")
    day_text = _REPLACEMENT_VERB.sub("", text).replace("替换", "")
    day = extract_explicit_target_day(day_text)
    match = re.fullmatch(
        r"(?:第\s*(?:[0-9]+|[一二两三四五六七八九十]{1,3})\s*天\s*)?(?:请|帮我)?\s*(?:把|将)\s*(.+?)\s*(?:替换成|替换为|换成|换为|改成|改为)\s*(.+?)[。.!！]?",
        text,
    )
    if not match:
        # Also accept the common inverse construction: 第N天用新地点替换原地点。
        match = re.fullmatch(
            r"(?:第\s*(?:[0-9]+|[一二两三四五六七八九十]{1,3})\s*天\s*)?(?:请|帮我)?\s*用\s*(.+?)\s*替换\s*(.+?)[。.!！]?",
            text,
        )
        if match:
            new_name, old_name = (part.strip(" \t\n，,。.!！：:") for part in match.groups())
        else:
            raise LocalReorderRejected("revision_intent_unresolved", "无法完整确认替换的原景点和新景点，请按‘第N天把原景点替换成新景点’说明；原版本保持不变。")
    else:
        old_name, new_name = (part.strip(" \t\n，,。.!！：:") for part in match.groups())
    if not day or not old_name or not new_name or old_name == new_name or any(
        re.search(pattern, name) for name in (old_name, new_name)
        for pattern in (r"[，,；;、]|和|以及|然后|再去|先去|预约|必须|并|同时|另外|顺便|新增|增加|添加|加入|删除|移除|替换|换成|改成|改为|\d{1,2}[:：]\d{2}",)
    ):
        raise LocalReorderRejected("revision_intent_unresolved", "请只指定一个目标日、原景点和新景点；原版本保持不变。")
    return day, {"kind": "replace", "old_poi_name": old_name, "new_poi_name": new_name}


async def replace_single_day_attraction(
    plan: Mapping[str, Any], planner_state: Mapping[str, Any], *, target_day: int,
    modification_notes: str, provider, poi_provider=None,
) -> LocalReorderResult:
    """Apply a single-day replacement as one frozen set change and one solve."""
    from app.planning.candidate_builder import (
        build_authoritative_candidates, candidate_pool_fingerprint,
        excluded_poi_names, resolve_constraint_coverage,
    )
    from app.planning.optimizer import daily_bounds_from_constraints
    from app.providers.amap.poi import search_attraction_pois_async
    import asyncio
    import os

    day, operation = parse_day_replacement(modification_notes)
    if day != target_day:
        raise LocalReorderRejected("revision_day_mismatch", "目标日期不一致，原版本保持不变。")
    changed_plan, changed_state = copy.deepcopy(dict(plan)), copy.deepcopy(dict(planner_state))
    target = next((item for item in changed_plan.get("days") or [] if int(item.get("day") or 0) == day), None)
    route = next((item for item in changed_state.get("route") or [] if int(item.get("day") or 0) == day), None)
    if target is None or route is None:
        raise LocalReorderRejected("revision_day_invalid", "找不到目标日，原版本保持不变。")
    old_name, new_name = operation["old_poi_name"], operation["new_poi_name"]
    names = [item.get("name") for item in route.get("spots") or []]
    plan_names = [item.get("name") for item in target.get("timeline") or [] if item.get("type") == "attraction"]
    if not names or len(names) != len(set(names)) or set(names) != set(plan_names) or len(plan_names) != len(names):
        raise LocalReorderRejected("local_reorder_unverified", "基准日缺少一致的已保存景点，原版本保持不变。")
    if old_name not in names:
        raise LocalReorderRejected("revision_poi_unresolved", "要替换的原景点不在目标日，原版本保持不变。")
    if new_name in names:
        raise LocalReorderRejected("local_set_no_change", "新景点已在目标日，原版本保持不变。")
    other_days = [item for item in changed_plan.get("days") or [] if int(item.get("day") or 0) != day]
    other_names = {
        item.get("name") for other in other_days for item in other.get("timeline") or []
        if item.get("type") == "attraction"
    }
    if new_name in other_names:
        raise LocalReorderRejected("local_set_cross_day_duplicate", "新景点已安排在其他日期，原版本保持不变，请确认是否扩大修改范围。")

    constraints = copy.deepcopy(changed_state.get("effective_constraints") or changed_plan.get("effective_constraints") or [])
    pool = changed_state.get("candidate_pool") or []
    pois = changed_state.get("pois") or []
    constraints, pool = _release_local_selection(
        constraints, pool, pois, name=old_name, day=day,
        days=int(changed_state.get("days") or len(changed_plan.get("days") or [])),
    )
    old_semantic = next((item for item in pool if item.get("poi_name") == old_name), None)
    if old_semantic and any(old_semantic.get(key) for key in ("must_visit", "fixed_day", "fixed_start_min", "must_be_first", "must_be_last")):
        raise LocalReorderRejected("local_set_conflict", "替换原景点与已冻结的必去、指定日或预约要求冲突，请先确认是否修改硬约束；原版本保持不变。")
    if any(old_name in (item.get("before_poi_names") or []) or (item.get("poi_name") == old_name and item.get("before_poi_names")) for item in pool):
        raise LocalReorderRejected("local_set_conflict", "替换原景点与已冻结的景点先后要求冲突，请先确认是否修改硬约束；原版本保持不变。")
    old_required, _ = build_authoritative_candidates(
        [], pois, days=int(changed_state.get("days") or len(changed_plan.get("days") or [])), constraints=constraints,
    )
    if any(item.poi_name == old_name and item.must_visit for item in old_required):
        raise LocalReorderRejected("local_set_conflict", "原景点属于已冻结的必去要求，不能直接替换；原版本保持不变。")
    if any(
        (item.poi_name == old_name and item.before_poi_names)
        or (item.poi_name != old_name and old_name in item.before_poi_names)
        for item in old_required
    ):
        raise LocalReorderRejected("local_set_conflict", "替换原景点与已冻结的景点先后要求冲突，请先确认是否修改硬约束；原版本保持不变。")

    semantic = next((item for item in pool if item.get("poi_name") == new_name), None)
    poi = next((item for item in pois if item.get("name") == new_name), None)
    other_ids = {
        item.get("id") or item.get("poi_id") for other in other_days
        for item in other.get("timeline") or [] if item.get("type") == "attraction"
    }
    if not semantic or not poi:
        try:
            raw = await asyncio.wait_for((poi_provider or search_attraction_pois_async)(
                str(changed_plan.get("destination") or changed_state.get("destination") or ""),
                os.getenv("AMAP_API_KEY", ""), keywords=new_name, types="",
            ), timeout=10)
        except Exception as exc:
            raise LocalReorderRejected("revision_authoritative_poi_unavailable", "权威地点服务暂时无法确认指定新景点，原版本保持不变。") from exc
        poi = _require_named_poi(raw, new_name)
        pois = [item for item in pois if item.get("name") != new_name] + [poi]
    if poi.get("id") and poi["id"] in other_ids:
        raise LocalReorderRejected("local_set_cross_day_duplicate", "权威新地点已安排在其他日期，原版本保持不变。")
    if new_name in excluded_poi_names(pois, constraints):
        raise LocalReorderRejected("local_set_conflict", "新景点与已冻结的避开要求冲突，原版本保持不变。")

    avoid_old = {"id": f"local-replace-avoid-day-{day}-{old_name}", "category": "attraction_preference", "polarity": "avoid", "value_text": f"不要去{old_name}", "source": "explicit_local_revision"}
    require_new = {"id": f"local-replace-require-day-{day}-{new_name}", "category": "attraction_preference", "polarity": "require", "value_text": f"必须去{new_name}", "source": "explicit_local_revision"}
    fixed_new = {"id": f"local-replace-fixed-day-{day}-{new_name}", "category": "schedule_preference", "polarity": "require", "value_text": f"第{day}天去{new_name}", "source": "explicit_local_revision"}
    constraints.extend((avoid_old, require_new))
    built, _ = build_authoritative_candidates(
        [], pois, days=int(changed_state.get("days") or len(changed_plan.get("days") or [])), constraints=constraints,
    )
    relevant = [item for item in constraints if item.get("polarity") in {"require", "avoid"} and (old_name in str(item.get("value_text") or "") or new_name in str(item.get("value_text") or ""))]
    coverage = resolve_constraint_coverage(constraints, [
        {"constraint_id": item.get("id") or item.get("fact_id"), "status": "unverified"} for item in relevant
    ], pois, built, days=int(changed_state.get("days") or len(changed_plan.get("days") or [])))
    if any(item.get("status") != "applied" for item in coverage):
        raise LocalReorderRejected("local_set_conflict", "替换后的冻结地点要求无法完整核验，原版本保持不变。", data={"constraint_failures": coverage})
    constraints.append(fixed_new)
    policy = next((item.model_dump(mode="json") for item in built if item.poi_name == new_name), None)
    if policy is None:
        raise LocalReorderRejected("revision_poi_unresolved", "指定新地点无法进入可核验候选，原版本保持不变。")
    if policy.get("fixed_day") not in (None, day):
        raise LocalReorderRejected("local_set_conflict", "新景点与已冻结的指定日要求冲突，原版本保持不变。")
    target_names = (set(names) - {old_name}) | {new_name}
    if any(
        (item.poi_name == new_name and any(successor not in target_names for successor in item.before_poi_names))
        or (item.poi_name not in target_names and new_name in item.before_poi_names)
        for item in built
    ):
        raise LocalReorderRejected("local_set_cross_day_constraint", "新景点涉及其他日期的先后要求，请先确认扩大修改范围；原版本保持不变。")
    if semantic and semantic.get("fixed_day") not in (None, day):
        raise LocalReorderRejected("local_set_conflict", "新景点与已冻结的指定日要求冲突，原版本保持不变。")
    new_semantic = {**policy, **(semantic or {}), "poi_name": new_name, "must_visit": True}
    new_semantic = _merge_hard_rules(new_semantic, policy)
    new_semantic["fixed_day"] = day
    pool = [item for item in pool if item.get("poi_name") not in {old_name, new_name}]
    pool.append(new_semantic)
    target["timeline"] = [item for item in target.get("timeline") or [] if not (item.get("type") == "attraction" and item.get("name") == old_name)]
    target["timeline"].append({**copy.deepcopy(poi), "type": "attraction"})
    route["spots"] = [item for item in route.get("spots") or [] if item.get("name") != old_name]
    route["spots"].append({"name": new_name})
    count = len(route["spots"])
    minimum, maximum = daily_bounds_from_constraints(constraints)
    if count < max(1, minimum or 0) or (maximum is not None and count > maximum):
        raise LocalReorderRejected("local_set_daily_count_conflict", "替换后的景点数量与每日数量要求冲突，原版本保持不变。")
    changed_state["candidate_pool"] = pool
    changed_state["pois"] = pois
    changed_state["effective_constraints"] = constraints
    changed_plan["effective_constraints"] = copy.deepcopy(constraints)
    changed_state["constraint_coverage"] = [*(changed_state.get("constraint_coverage") or []), *[
        {"constraint_id": item["id"], "status": "applied", "stages": ["candidate_builder", "local_replace_solver", "time_check"]}
        for item in (avoid_old, require_new, fixed_new)
    ]]
    result = await reorder_single_day(
        changed_plan, changed_state, target_day=day,
        modification_notes=f"重排第{day}天", provider=provider,
    )
    scope = {"kind": "local_replace", "day": day, "operation": operation}
    result.plan["revision_scope"] = scope
    result.planner_state["revision_scope"] = scope
    result.planner_state["modification_notes"] = modification_notes
    result.planner_state["candidate_pool_fingerprint"] = candidate_pool_fingerprint(pool)
    result.plan["constraint_coverage"] = copy.deepcopy(result.planner_state["constraint_coverage"])
    result.plan["quality_report"]["validation_scope"] = scope
    result.planner_state["quality_report"]["validation_scope"] = scope
    result.verification["operation"] = operation
    _refresh_changed_day_theme(result, day)
    return result

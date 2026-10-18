from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any


class FullRevisionIntentError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        self.public_message = message
        super().__init__(code)


@dataclass(frozen=True)
class FullRevisionIntent:
    destination: str | None
    start_date: date | None
    end_date: date | None
    constraints: tuple[dict[str, Any], ...]
    moved_pois: tuple[str, ...]
    summary: str


_SCOPE_MARKER = re.compile(r"全量|全程|整体|全行程|跨日|整趟|整段行程|(?:目的地|出行)?日期\s*(?:改为|调整为|换为)|目的地\s*(?:改为|改到|调整为|换到)")
_DESTINATION = re.compile(r"(?:目的地|目的地城市)\s*(?:改为|改到|调整为|换到)\s*([^，,；;。\s]+)")
_DATE_RANGE = re.compile(r"(?:出行)?日期\s*(?:改为|调整为|换为)\s*(\d{4}-\d{2}-\d{2})\s*(?:至|到|~|—|-)\s*(\d{4}-\d{2}-\d{2})")
_REQUIRE = re.compile(r"(?:必须去|一定要去|必去)\s*([^，,；;。\s]+)")
_AVOID = re.compile(r"(?:避开|不要去|不去)\s*([^，,；;。\s]+)")
_DAILY_COUNT = re.compile(r"(?:每天|每日)\s*(?P<operator>安排|最多|至多|控制在)?\s*(?P<count>\d+)\s*(?:个|处)?\s*(?:景点|地点)")
_MOVE = re.compile(r"把\s*([^，,；;。\s]+)\s*从\s*第?(\d+)天\s*(?:调整|移动|挪|改)到\s*第?(\d+)天")



_EXACT_ATTRACTION_TARGET = re.compile(
    r"(?:\u5fc5\u987b\u53bb|\u4e00\u5b9a\u8981\u53bb|\u5fc5\u53bb|\u907f\u5f00|\u4e0d\u8981\u53bb|\u4e0d\u53bb)\s*(.+)"
)
_EXACT_FIXED_DAY_TARGET = re.compile(
    r"第\s*\d+\s*天(?:必须去|一定要去|必去|去|安排)(.+)"
)
_EXACT_DAILY_COUNT = re.compile(
    r"(?:\u6bcf\u5929|\u6bcf\u65e5)(?:(?:\u6700\u591a|\u81f3\u591a|\u4e0d\u8d85\u8fc7|\u63a7\u5236\u5728|\u5b89\u6392))?\s*[0-9\u96f6\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341]+\s*(?:\u4e2a|\u5904)?\s*(?:\u666f\u70b9|\u5730\u70b9)"
)


def supersedes_existing_constraint(existing: dict[str, Any], intent: FullRevisionIntent) -> bool:
    category = existing.get("category")
    value = str(existing.get("value_text") or "").strip()
    polarity = existing.get("polarity")
    replaced_schedules = {name.casefold() for name in intent.moved_pois}
    replaced_schedules.update(
        found.group(1).strip().casefold()
        for item in intent.constraints
        if item.get("category") == "attraction_preference" and item.get("polarity") == "avoid"
        and (found := _EXACT_ATTRACTION_TARGET.fullmatch(str(item.get("value_text") or "")))
    )
    if category == "attraction_preference" and polarity == "require":
        scheduled = _EXACT_FIXED_DAY_TARGET.fullmatch(value)
        if scheduled and scheduled.group(1).strip().casefold() in replaced_schedules:
            return True
    if category == "attraction_preference" and polarity in {"require", "avoid"}:
        match = _EXACT_ATTRACTION_TARGET.fullmatch(value)
        if not match:
            return False
        target = match.group(1).strip().casefold()
        updated_targets = {
            found.group(1).strip().casefold()
            for item in intent.constraints
            if item.get("category") == "attraction_preference"
            and (found := _EXACT_ATTRACTION_TARGET.fullmatch(str(item.get("value_text") or "")))
        }
        return target in updated_targets
    if category == "schedule_preference" and polarity == "require":
        match = _EXACT_FIXED_DAY_TARGET.fullmatch(value)
        return bool(match and match.group(1).strip().casefold() in replaced_schedules)
    if category == "travel_pace" and polarity == "require" and any(
        item.get("category") == "travel_pace" for item in intent.constraints
    ):
        return bool(_EXACT_DAILY_COUNT.fullmatch(value))
    return False


def is_full_revision_request(notes: str) -> bool:
    text = str(notes or "")
    return bool(_SCOPE_MARKER.search(text) or _MOVE.search(text))


def parse_confirmed_full_revision(notes: str) -> FullRevisionIntent:
    text = str(notes or "").strip()
    unchanged_or_confirmed = {
        "其余硬性要求保留", "其余要求保留", "其他要求不变", "其余要求不变",
        "确认修改并开始", "确认修改", "确认开始", "开始修改",
    }
    # Only complete, standalone acknowledgement clauses can be consumed.
    # Cancellation and unknown requirements must still reach strict validation.
    text = "，".join(clause.strip() for clause in re.split(r"[，,；;。.!！]", text)
                    if clause.strip() and clause.strip() not in unchanged_or_confirmed)
    if not (_SCOPE_MARKER.search(text) or _MOVE.search(text)):
        raise FullRevisionIntentError(
            "revision_scope_confirmation_required",
            "这项修改可能影响多天或整段行程。请明确确认‘跨日’或‘全量修改’，并说明具体变更；原版本保持不变。",
        )
    singleton_patterns = (
        ("destination", _DESTINATION),
        ("date range", _DATE_RANGE),
        ("daily attraction count", _DAILY_COUNT),
        ("cross-day move", _MOVE),
    )
    for label, pattern in singleton_patterns:
        if len(list(pattern.finditer(text))) > 1:
            raise FullRevisionIntentError(
                "revision_intent_unresolved",
                f"全量修改中包含多个{label}要求，请分次提交；原版本保持不变。",
            )
    destination_match = _DESTINATION.search(text)
    date_match = _DATE_RANGE.search(text)
    destination = destination_match.group(1).strip() if destination_match else None
    start = end = None
    if date_match:
        try:
            start, end = date.fromisoformat(date_match.group(1)), date.fromisoformat(date_match.group(2))
        except ValueError as exc:
            raise FullRevisionIntentError("revision_intent_unresolved", "日期格式无效，请使用YYYY-MM-DD；原版本保持不变。") from exc
        if end < start:
            raise FullRevisionIntentError("revision_intent_unresolved", "结束日期早于开始日期，请确认完整日期范围；原版本保持不变。")
    constraints: list[dict[str, Any]] = []
    moved_pois: list[str] = []
    for polarity, pattern in (("require", _REQUIRE), ("avoid", _AVOID)):
        for index, match in enumerate(pattern.finditer(text)):
            name = match.group(1).strip()
            constraints.append({
                "id": f"full-revision-{polarity}-{index + 1}",
                "category": "attraction_preference", "polarity": polarity,
                "value_text": ("必须去" if polarity == "require" else "避开") + name,
                "source": "confirmed_full_revision",
            })
    count_match = _DAILY_COUNT.search(text)
    if count_match:
        count = int(count_match.group("count"))
        if count < 1 or count > 8:
            raise FullRevisionIntentError("revision_intent_unresolved", "每日景点数量超出可验证范围，请重新确认；原版本保持不变。")
        upper_bound = count_match.group("operator") in {"最多", "至多", "控制在"}
        constraints.append({
            "id": "full-revision-daily-count", "category": "travel_pace",
            "polarity": "require", "value_text": f"每天{'最多' if upper_bound else ''}{count}个景点",
            "source": "confirmed_full_revision",
        })
    move = _MOVE.search(text)
    if move:
        name, old_day, new_day = move.groups()
        moved_pois.append(name)
        if int(old_day) == int(new_day):
            raise FullRevisionIntentError("revision_intent_unresolved", "跨日调整必须指定两个不同日期；原版本保持不变。")
        constraints.append({
            "id": "full-revision-cross-day-move", "category": "schedule_preference",
            "polarity": "require", "value_text": f"第{new_day}天去{name}",
            "source": "confirmed_full_revision",
        })
    recognized = []
    for pattern in (_DESTINATION, _DATE_RANGE, _REQUIRE, _AVOID, _DAILY_COUNT, _MOVE):
        recognized.extend(match.span() for match in pattern.finditer(text))
    residual = list(text)
    for begin, finish in sorted(recognized, reverse=True):
        residual[begin:finish] = [" "] * (finish - begin)
    unparsed = "".join(residual)
    unparsed = _SCOPE_MARKER.sub("", unparsed)
    unparsed = re.sub(r"[\s，,；;。.!！:：、请帮我想把行程路线方案调整修改一下以及同时并且的]", "", unparsed)
    if unparsed:
        raise FullRevisionIntentError(
            "revision_intent_unresolved",
            "全量修改中包含无法完整转换为可验证约束的内容，请拆开说明；原版本保持不变。",
        )
    if not (destination or start or constraints):
        raise FullRevisionIntentError(
            "revision_intent_unresolved",
            "已确认全量修改范围，但无法把具体要求完整转换为规划约束。请明确目的地、日期、必去/避开地点、跨日移动或每日数量；原版本保持不变。",
        )
    summary = "; ".join(
        part for part in (
            f"目的地改为{destination}" if destination else None,
            f"日期改为{start.isoformat()}至{end.isoformat()}" if start else None,
            *(item["value_text"] for item in constraints),
        ) if part
    )
    return FullRevisionIntent(destination, start, end, tuple(constraints), tuple(moved_pois), summary)

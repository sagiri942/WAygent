"""Complete, model-free daily attraction count expressions shared by Brief and solver."""
from __future__ import annotations

import re
from typing import Any, Mapping, Sequence


_CN_NUMBERS = {
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}


def _count_number(value: str) -> int | None:
    if value.isdigit():
        return int(value)
    return _CN_NUMBERS.get(value)


def daily_bound_for_constraint(item: Mapping[str, Any]) -> tuple[int | None, int | None] | None:
    """Recognize a complete per-day count entry, never a substring of it."""
    if item.get("polarity") != "require":
        return None
    text = str(item.get("value_text") or "").strip()
    if not re.match(r"(?:每天|每日|一天)", text):
        return None
    clauses = re.split(r"[，,；;]|并且|且", text)
    minimum = maximum = None
    pattern = r"(?:每天|每日|一天)?\s*(最多|至多|不超过|上限|至少|不少于|下限)?\s*(\d+|[一二两三四五六七八九十])\s*(?:个|处)?景点[。.!！]?"
    for clause in clauses:
        match = re.fullmatch(pattern, clause.strip())
        if not match:
            return None
        operator, number = match.groups()
        count = _count_number(number)
        if count is None or count < 1:
            return None
        if operator in {"最多", "至多", "不超过", "上限"}:
            maximum = count if maximum is None else min(maximum, count)
        elif operator in {"至少", "不少于", "下限"}:
            minimum = count if minimum is None else max(minimum, count)
        else:
            minimum = count if minimum is None else max(minimum, count)
            maximum = count if maximum is None else min(maximum, count)
    return minimum, maximum


def daily_bounds_from_constraints(
    constraints: Sequence[Mapping[str, Any]],
) -> tuple[int | None, int | None]:
    """Aggregate only fully recognized confirmed per-day count entries."""
    user_min = user_max = None
    for item in constraints:
        parsed = daily_bound_for_constraint(item)
        if parsed is None:
            continue
        minimum, maximum = parsed
        if minimum is not None:
            user_min = minimum if user_min is None else max(user_min, minimum)
        if maximum is not None:
            user_max = maximum if user_max is None else min(user_max, maximum)
    return user_min, user_max

"""Shared PlanningBrief readiness rules.

The chat graph and persistence layer both use these pure helpers so a brief
cannot be presented as ready when the formal planning graph would immediately
interrupt for missing calendar dates.
"""

from __future__ import annotations

from datetime import date
from typing import Any


def _iso_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value or "").strip())
    except ValueError:
        return None


def required_brief_fields(data: dict[str, Any]) -> list[str]:
    """Return missing or invalid fields required before formal planning."""
    missing: list[str] = []
    if not str(data.get("destination") or "").strip():
        missing.append("destination")

    start = _iso_date(data.get("start_date"))
    end = _iso_date(data.get("end_date"))
    if start is None:
        missing.append("start_date")
    if end is None:
        missing.append("end_date")
    if start is not None and end is not None and end < start:
        missing.append("date_range")
    if data.get("trip_focus") not in {"sights_first", "food_first", "balanced"}:
        missing.append("trip_focus")
    from app.core.transport import yuan_cents, total_budget_cents
    total = data.get("trip_budget") or data.get("budget")
    if str(total or "").strip() in {"不限", "不限制", "无限", "unlimited"}:
        total = None
    allowance = data.get("transport_budget")
    if str(total or "").strip() and allowance in (None, ""):
        missing.append("transport_budget")
    elif allowance not in (None, ""):
        try:
            amount = yuan_cents(allowance)
            if total:
                try:
                    total_amount = total_budget_cents(total)
                except ValueError:
                    total_amount = None
                    missing.append("trip_budget")
                if total_amount is not None and (amount is None or amount > total_amount):
                    missing.append("transport_budget")
        except ValueError:
            missing.append("transport_budget")
    return missing


_FIELD_LABELS = {
    "destination": "目的地",
    "start_date": "开始日期",
    "end_date": "结束日期或游玩天数",
    "date_range": "有效日期范围",
    "trip_focus": "旅行侧重点",
    "transport_budget": "景点间交通预算（单独额度，计入总预算，不能超过总预算）",
    "trip_budget": "明确的旅行总预算金额（数字元）",
}


def required_input_interrupt(missing_fields: list[str]) -> dict[str, Any]:
    """Build the stable public interrupt contract for the main agent."""
    missing = list(dict.fromkeys(missing_fields))
    if missing == ["trip_focus"]:
        return {
            "question": "这趟更想景点为主、吃吃喝喝为主，还是两者均衡？",
            "missing_fields": missing,
            "input_schema": {
                "type": "string",
                "enum": ["景点为主", "吃吃喝喝为主", "均衡安排"],
            },
        }
    labels = "、".join(_FIELD_LABELS.get(item, item) for item in missing)
    return {
        "question": f"为了继续规划，还需要补充：{labels}。",
        "missing_fields": missing,
        "input_schema": {"type": "string", "minLength": 1},
    }

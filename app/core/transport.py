"""Transport choices and exact budget allocation for a fixed sequence of legs."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_CEILING
import re
from typing import Any, Literal

from pydantic import BaseModel, Field

TransportMode = Literal["walking", "bus", "subway", "transit", "taxi"]
ALLOWED_MODES = {"walking", "bus", "subway", "taxi"}


def constraint_modes(constraint):
    """Recognize complete mode restrictions; unknown hard expressions fail closed."""
    text = re.sub(r"[\s，,。！!]+", "", str(constraint.get("value_text") or ""))
    modes = {"步行": {"walking"}, "走路": {"walking"}, "公交": {"bus"}, "公交车": {"bus"},
             "地铁": {"subway"}, "公共交通": {"bus", "subway"},
             "打车": {"taxi"}, "出租车": {"taxi"}, "网约车": {"taxi"}}
    negative = re.fullmatch(r"(?:不要|不能|禁止|避免|不想|不)(?:乘坐|乘|坐|使用)?(步行|走路|公交车?|地铁|公共交通|打车|出租车|网约车)", text)
    positive = re.fullmatch(r"(?:只允许|只能|必须|全程|仅|只)(?:乘坐|乘|坐|使用)?(步行|走路|公交车?|地铁|公共交通|打车|出租车|网约车)", text)
    if negative:
        return "avoid", modes[negative[1]]
    if positive:
        return "require", modes[positive[1]]
    if text in modes and constraint.get("polarity") in {"avoid", "require"}:
        return constraint["polarity"], modes[text]
    return None


def allowed_transport_modes(constraints, configured=None):
    allowed = set(configured if configured is not None else ALLOWED_MODES)
    for constraint in constraints:
        if constraint.get("category") != "transport_preference":
            continue
        restriction = constraint_modes(constraint)
        if restriction is None:
            if constraint.get("polarity") in {"require", "avoid"}:
                raise TransportUnavailable("交通硬要求暂时无法核验")
            continue
        action, modes = restriction
        allowed = allowed - modes if action == "avoid" else allowed & modes
    if not allowed:
        raise TransportUnavailable("允许的交通方式为空")
    return allowed


class TransportUnavailable(ValueError):
    pass


class TransportOption(BaseModel):
    mode: TransportMode
    minutes: int = Field(gt=0)
    cost_cents: int | None = Field(default=None, ge=0)
    segments: list[dict[str, Any]] = Field(default_factory=list)
    buffer_min: int = Field(default=0, ge=0)
    queried_at: str = ""
    source: str = "amap_estimate"


def yuan_cents(value: Any) -> int | None:
    if value is None or str(value).strip() in {"", "不限", "不限制", "无限", "unlimited"}:
        return None
    try:
        amount = Decimal(str(value).strip().removesuffix("元"))
    except InvalidOperation as exc:
        raise ValueError("金额需要填写非负数字或不限") from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError("金额需要填写非负数字或不限")
    return int((amount * 100).to_integral_value(rounding=ROUND_CEILING))


def total_budget_cents(value):
    """Accept a single explicit total; ambiguous ranges need clarification."""
    try:
        return yuan_cents(value)
    except ValueError:
        match = re.fullmatch(r"(?:本次|总共|总|旅行|预算|约|大约|人民币|￥|¥|\s)*([0-9]+(?:\.[0-9]+)?)\s*元?(?:左右|以内|上下)?", str(value).strip())
        if not match:
            raise ValueError("旅行总预算需要明确的数字金额") from None
        return yuan_cents(match[1])


def choose_transport(options_by_leg: list[list[TransportOption]], *, budget_cents: int | None):
    """Pareto search: fastest complete combination under the whole-trip cap."""
    frontier = [(0, 0, [])]
    for options in options_by_leg:
        expanded = []
        for cost, minutes, selected in frontier:
            for option in options:
                if budget_cents is not None and option.cost_cents is None:
                    continue
                revised_cost = cost + (option.cost_cents or 0)
                if budget_cents is None or revised_cost <= budget_cents:
                    expanded.append((revised_cost, minutes + option.minutes, [*selected, option]))
        if not expanded:
            raise TransportUnavailable("没有费用可核验且符合交通预算的完整组合")
        if budget_cents is None:
            frontier = [min(expanded, key=lambda item: (item[1], item[0]))]
            continue
        frontier, fastest = [], float("inf")
        for entry in sorted(expanded, key=lambda item: (item[0], item[1])):
            if entry[1] < fastest:
                frontier.append(entry)
                fastest = entry[1]
    return min(frontier, key=lambda item: (item[1], item[0]))[2]

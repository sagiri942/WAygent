"""Shared interpretation of persisted local itinerary revision scope."""

from collections.abc import Mapping
from typing import Any

LOCAL_REVISION_KINDS = frozenset({"local_reorder", "local_add_remove", "local_replace"})


def local_revision_day(planner_state: Mapping[str, Any]) -> int | None:
    scope = planner_state.get("revision_scope") or {}
    if scope.get("kind") in LOCAL_REVISION_KINDS and scope.get("day") is not None:
        return int(scope["day"])
    return None

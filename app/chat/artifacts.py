"""Validated, bounded attachments persisted with conversation messages."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class _ArtifactModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ItineraryCard(_ArtifactModel):
    itinerary_id: str = Field(min_length=1, max_length=100)
    root_id: str = Field(min_length=1, max_length=100)
    version: int = Field(ge=1)
    destination: str = Field(default="", max_length=120)
    duration_days: int | None = Field(default=None, ge=1, le=60)
    start_date: str | None = Field(default=None, max_length=20)
    end_date: str | None = Field(default=None, max_length=20)
    created_at: str = Field(default="", max_length=80)
    is_modified: bool = False
    highlights: list[str] = Field(default_factory=list, max_length=4)


class ItineraryCollectionArtifact(_ArtifactModel):
    type: Literal["itinerary_collection"] = "itinerary_collection"
    title: str = Field(default="找到这些保存的方案", max_length=120)
    match_kind: Literal["exact", "near", "mixed"] = "exact"
    items: list[ItineraryCard] = Field(default_factory=list, max_length=5)


MessageArtifact = ItineraryCollectionArtifact
_artifact_adapter = TypeAdapter(MessageArtifact)


def validate_message_artifacts(value: Any) -> list[dict[str, Any]]:
    """Return JSON-safe public artifacts or reject oversized/unknown payloads."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("message artifacts must be a list")
    if len(value) > 5:
        raise ValueError("a message can contain at most five artifacts")
    return [
        _artifact_adapter.validate_python(item).model_dump(mode="json")
        for item in value
    ]

"""Structured travel-memory profile APIs."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Header, HTTPException, Depends
from pydantic import BaseModel, Field

from app.core.auth import decode_token
from app.core.identity import SQLiteProfiles, UnsupportedProfileOperation
from app.core.travel_memory import MemoryNotFound, MemoryRepository


router = APIRouter(prefix="/api", tags=["profile"])
memories = MemoryRepository()
profiles = SQLiteProfiles(memories)


def configure_profile_backend(database) -> None:
    global memories, profiles
    if database.engine.dialect.name != "mysql":
        raise RuntimeError("Production profile storage requires MySQL")
    from app.core.identity_storage import CoreProfiles
    from app.core.mysql_travel_memory import CoreMemoryRepository
    memories = CoreMemoryRepository(database)
    profiles = CoreProfiles(database, memories)

def get_memories():
    return memories

def get_profiles():
    return profiles


MemoryCategory = Literal[
    "attraction_preference", "food_preference", "dietary_requirement",
    "travel_pace", "budget_style", "transport_preference",
    "accommodation_preference", "schedule_preference", "companion_context",
    "accessibility_need", "destination_history", "other_travel_preference",
]
MemoryPolarity = Literal["prefer", "avoid", "require", "fact"]
MemoryScope = Literal["global", "destination", "companion", "destination_companion"]


class MemoryCreate(BaseModel):
    category: MemoryCategory
    value_text: str = Field(min_length=1, max_length=500)
    polarity: MemoryPolarity = "fact"
    scope_type: MemoryScope = "global"
    scope_key: dict[str, str] = Field(default_factory=dict)


class MemoryPatch(BaseModel):
    category: MemoryCategory | None = None
    value_text: str | None = Field(default=None, min_length=1, max_length=500)
    polarity: MemoryPolarity | None = None
    scope_type: MemoryScope | None = None
    scope_key: dict[str, str] | None = None


def _require_user(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "未登录")
    user_id = decode_token(authorization[7:])
    if not user_id:
        raise HTTPException(401, "token 无效或已过期")
    return user_id


@router.get("/profile")
def get_profile(authorization: str | None = Header(default=None), profiles=Depends(get_profiles)):
    user_id = _require_user(authorization)
    return profiles.read(user_id)


@router.post("/memories", status_code=201)
def create_memory(
    body: MemoryCreate,
    authorization: str | None = Header(default=None),
    memories=Depends(get_memories),
):
    try:
        return memories.create(
            _require_user(authorization),
            **body.model_dump(),
            status="active",
            source_kind="manual",
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.patch("/memories/{fact_id}")
def update_memory(
    fact_id: str,
    body: MemoryPatch,
    authorization: str | None = Header(default=None),
    memories=Depends(get_memories),
):
    try:
        return memories.replace(
            _require_user(authorization), fact_id, **body.model_dump(exclude_none=True)
        )
    except UnsupportedProfileOperation as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except MemoryNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/memories/{fact_id}/approve")
def approve_memory(
    fact_id: str,
    authorization: str | None = Header(default=None),
    memories=Depends(get_memories),
):
    try:
        return memories.approve(_require_user(authorization), fact_id)
    except UnsupportedProfileOperation as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except MemoryNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.delete("/memories/{fact_id}")
def delete_memory(
    fact_id: str,
    authorization: str | None = Header(default=None),
    memories=Depends(get_memories),
):
    try:
        return memories.delete(_require_user(authorization), fact_id)
    except UnsupportedProfileOperation as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except MemoryNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

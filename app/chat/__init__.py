"""Conversational travel planning graph and services.

The package used to import public objects eagerly.  Runtime repositories
also import ``app.chat.artifacts`` and Python executes this module first, which
created a repositories -> chat.service -> repositories import cycle.  Keep the
public API while loading the service only when it is requested.
"""

from __future__ import annotations

from typing import Any

__all__ = ["ChatService"]


def __getattr__(name: str) -> Any:
    if name == "ChatService":
        from app.chat.service import ChatService

        return ChatService
    raise AttributeError(name)

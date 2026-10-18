"""LangChain create_agent graph factory, for the supported ReAct conversation entry."""

from __future__ import annotations

import os
from typing import Any

from app.chat.models import MainAgentContext
from app.chat.prompts import MAIN_AGENT_SYSTEM


def build_main_agent_graph(
    tools: list[Any], *, checkpointer: Any = None, llm: Any = None
) -> Any:
    from langchain.agents import create_agent

    if llm is None:
        from app.llm.factory import build_chat_llm
        llm = build_chat_llm(
            provider="deepseek",
            model=os.getenv("MAIN_AGENT_MODEL") or None,
            temperature=0,
            thinking=True,
            reasoning_effort=os.getenv("MAIN_AGENT_REASONING_EFFORT", "high"),
        )
    return create_agent(
        model=llm,
        tools=tools,
        system_prompt=MAIN_AGENT_SYSTEM,
        context_schema=MainAgentContext,
        checkpointer=checkpointer,
        name="waygent_main_agent",
    )

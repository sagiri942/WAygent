"""LangChain tool definitions for the owner-scoped WAygent main Agent."""

from __future__ import annotations

import json
from typing import Literal

from langchain.tools import ToolRuntime, tool

from app.chat.models import MainAgentContext, PlanningBriefPatch
from app.chat.tool_service import MainAgentToolService


TOOL_PRESENTATION = {
    "get_travel_memory": ("memory_lookup", "正在查看你的旅行偏好"),
    "get_planning_context": ("planning_context", "正在核对当前旅行需求"),
    "find_saved_itineraries": ("itinerary_search", "正在查找保存的旅行方案"),
    "get_saved_itinerary": ("itinerary_read", "正在读取旅行方案"),
    "update_current_brief": ("brief_update", "正在整理旅行需求"),
    "submit_current_brief": ("planning_submit", "正在生成完整行程"),
    "start_revision": ("revision_start", "正在调整行程"),
    "control_run": ("run_control", "正在更新任务状态"),
}


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, ValueError):
        return _json({"ok": False, "code": "invalid_request", "message": "查询条件不符合要求，请调整后重试。"})
    return _json({"ok": False, "code": "resource_unavailable", "message": "请求的旅行信息不可用。"})


def _progress(runtime: object, name: str, stage: str) -> None:
    writer = getattr(runtime, "stream_writer", None)
    if not callable(writer):
        return
    activity_type, label = TOOL_PRESENTATION[name]
    writer({
        "kind": "agent.activity.progress",
        "activity_id": f"tool:{getattr(runtime, 'tool_call_id', '')}",
        "activity_type": activity_type,
        "label": label,
        "stage": stage,
        "stats": {},
    })


def build_main_agent_tools(service: MainAgentToolService):
    """Build the fixed, identity-free public tool definitions."""

    @tool
    def get_travel_memory(
        categories: list[str] | None = None,
        destination: str | None = None,
        companion: str | None = None,
        polarities: list[Literal["prefer", "avoid", "require", "fact"]] | None = None,
        include_provenance: bool = False,
        limit: int = 20,
        *,
        runtime: ToolRuntime[MainAgentContext],
    ) -> str:
        """读取本会话冻结的 active 长期旅行记忆。回答偏好、旅行者特点、去过哪里时使用；去过哪里只看 destination_history。"""
        _progress(runtime, "get_travel_memory", "reading")
        try:
            return _json(service.get_travel_memory(
                runtime.context, categories=categories, destination=destination,
                companion=companion, polarities=polarities,
                include_provenance=include_provenance, limit=limit,
            ))
        except Exception as exc:
            return _safe_error(exc)

    @tool
    def get_planning_context(*, runtime: ToolRuntime[MainAgentContext]) -> str:
        """读取今天、时区、当前 Brief、缺失字段、活动任务和显式绑定目标；涉及相对日期或当前规划状态时使用。"""
        _progress(runtime, "get_planning_context", "reading")
        try:
            return _json(service.get_planning_context(runtime.context))
        except Exception as exc:
            return _safe_error(exc)

    @tool(response_format="content_and_artifact")
    def find_saved_itineraries(
        destination: str | None = None,
        duration_days: int | None = None,
        travel_start_from: str | None = None,
        travel_start_to: str | None = None,
        include_revisions: bool = False,
        limit: int = 5,
        *,
        runtime: ToolRuntime[MainAgentContext],
    ) -> tuple[str, dict | None]:
        """查询当前用户保存的旅行方案。用户说“找出某地几日游方案”时使用，默认每组只返回最新版。"""
        _progress(runtime, "find_saved_itineraries", "searching")
        try:
            content, artifact = service.find_saved_itineraries(
                runtime.context, destination=destination, duration_days=duration_days,
                travel_start_from=travel_start_from, travel_start_to=travel_start_to,
                include_revisions=include_revisions, limit=limit,
            )
            return _json(content), artifact
        except Exception as exc:
            return _safe_error(exc), None

    @tool
    def get_saved_itinerary(
        itinerary_id: str,
        day: int | None = None,
        *,
        runtime: ToolRuntime[MainAgentContext],
    ) -> str:
        """读取一个已保存方案的摘要，或只读取指定天。不要用它把完整多日方案塞入上下文。"""
        _progress(runtime, "get_saved_itinerary", "reading")
        try:
            return _json(service.get_saved_itinerary(runtime.context, itinerary_id, day))
        except Exception as exc:
            return _safe_error(exc)

    @tool
    async def update_current_brief(
        patch: PlanningBriefPatch,
        *,
        runtime: ToolRuntime[MainAgentContext],
    ) -> str:
        """把用户本轮明确提供或纠正的旅行字段更新到当前唯一 Brief；普通规划补充可直接执行。"""
        _progress(runtime, "update_current_brief", "validating")
        result = await service.update_current_brief(
            runtime.context, patch.model_dump(exclude_none=True, exclude_unset=True)
        )
        return _json(result.model_dump(mode="json"))

    @tool
    async def submit_current_brief(*, runtime: ToolRuntime[MainAgentContext]) -> str:
        """仅当用户明确要求生成方案时调用；在当前 Chat Run 内执行 Planner task，并持续产生内部进度事件。"""
        _progress(runtime, "submit_current_brief", "submitting")
        return _json((await service.submit_current_brief(runtime.context)).model_dump(mode="json"))

    @tool
    async def start_revision(
        itinerary_id: str,
        modification_notes: str,
        transport_budget: str | None = None,
        *,
        runtime: ToolRuntime[MainAgentContext],
    ) -> str:
        """修改唯一已保存方案并直接保存关联新版本。支持单日重排/新增/删除/替换及明确确认的全量修改。modification_notes 使用本轮完整原话，不加日期、引用或范围说明；例如‘第2天新增拙政园’、‘第2天用虎丘替换寒山寺’、‘全量修改：每天最多2个景点’。多轮确认时只整理已确认的完整操作，不能丢弃附加硬约束。"""
        _progress(runtime, "start_revision", "creating")
        # A detail-page request already selected its target. History-derived
        # model arguments cannot replace that server-injected binding; the
        # service still checks it against the persisted request and ownership.
        target_id = runtime.context.related_itinerary_id or itinerary_id
        return _json((await service.start_revision(
            runtime.context, target_id, modification_notes,
            **({"transport_budget": transport_budget} if transport_budget is not None else {}),
        )).model_dump(mode="json"))

    @tool
    async def control_run(
        run_id: str,
        action: Literal["cancel", "retry"],
        requires_confirmation: bool = False,
        *,
        runtime: ToolRuntime[MainAgentContext],
    ) -> str:
        """停止或重试当前用户的任务。只有明确指令且目标唯一才执行；仍需用户确认时 requires_confirmation=true，仅返回确认提示。"""
        _progress(runtime, "control_run", "updating")
        return _json((await service.control_run(
            runtime.context, run_id, action, requires_confirmation=requires_confirmation
        )).model_dump(mode="json"))

    return [
        get_travel_memory, get_planning_context, find_saved_itineraries,
        get_saved_itinerary, update_current_brief, submit_current_brief,
        start_revision, control_run,
    ]

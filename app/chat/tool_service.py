"""Owner-scoped deterministic operations exposed to the main Agent."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from langgraph.types import interrupt
from zoneinfo import ZoneInfo

from app.chat.artifacts import ItineraryCard, ItineraryCollectionArtifact
from app.chat.models import AgentToolResult, MainAgentContext
from app.core.planning_brief import required_brief_fields, required_input_interrupt
from app.core.itinerary_storage import itinerary_connection
from app.core.memory import get_itinerary_record, list_itinerary_records
from app.runtime.models import RunKind
from app.runtime.repositories import OwnedResourceNotFound


MEMORY_CATEGORIES = {
    "attraction_preference", "food_preference", "dietary_requirement",
    "travel_pace", "budget_style", "transport_preference",
    "accommodation_preference", "schedule_preference", "companion_context",
    "accessibility_need", "destination_history", "other_travel_preference",
}
MEMORY_POLARITIES = {"prefer", "avoid", "require", "fact"}


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _date_days(start: Any, end: Any) -> int | None:
    try:
        return (date.fromisoformat(str(end)) - date.fromisoformat(str(start))).days + 1
    except (TypeError, ValueError):
        return None


def _public_run(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": run["id"], "kind": run["kind"], "status": run["status"],
        "conversation_id": run.get("conversation_id"),
        "result_itinerary_id": run.get("result_itinerary_id"),
        "created_at": run.get("created_at"), "updated_at": run.get("updated_at"),
    }


class MainAgentToolService:
    def __init__(self, chat_service: Any, db_path: str | Path | None = None):
        self.chat = chat_service
        self.db_path = db_path if db_path is not None else chat_service.manager.db_path

    def get_travel_memory(
        self,
        context: MainAgentContext,
        *,
        categories: list[str] | None = None,
        destination: str | None = None,
        companion: str | None = None,
        polarities: list[str] | None = None,
        include_provenance: bool = False,
        limit: int = 20,
    ) -> dict[str, Any]:
        chosen_categories = set(categories or MEMORY_CATEGORIES)
        chosen_polarities = set(polarities or MEMORY_POLARITIES)
        if not chosen_categories <= MEMORY_CATEGORIES:
            raise ValueError("invalid memory category")
        if not chosen_polarities <= MEMORY_POLARITIES:
            raise ValueError("invalid memory polarity")
        state = self.chat.memory_context.memories.get(
            context.user_id, context.conversation_id
        )
        if int(state["profile_revision"]) != context.memory_revision:
            raise ValueError("frozen memory revision mismatch")
        destination_key = _clean_text(destination)
        companion_key = _clean_text(companion)
        facts: list[dict[str, Any]] = []
        for fact in state.get("profile_snapshot") or []:
            if fact.get("status") != "active":
                continue
            if fact.get("category") not in chosen_categories:
                continue
            if fact.get("polarity") not in chosen_polarities:
                continue
            scope = fact.get("scope_key") or {}
            if destination_key and fact.get("scope_type") in {"destination", "destination_companion"}:
                if _clean_text(scope.get("destination")) != destination_key:
                    continue
            if companion_key and fact.get("scope_type") in {"companion", "destination_companion"}:
                if companion_key not in _clean_text(scope.get("companion")):
                    continue
            item = {
                "id": fact.get("id"), "category": fact.get("category"),
                "value_text": fact.get("value_text"), "polarity": fact.get("polarity"),
                "scope_type": fact.get("scope_type"), "scope_key": scope,
            }
            if include_provenance:
                item["provenance"] = {
                    "source_kind": fact.get("source_kind"),
                    "source_conversation_id": fact.get("source_conversation_id"),
                    "evidence_sequences": list(fact.get("evidence_sequences") or [])[:20],
                }
            facts.append(item)
        facts.sort(key=lambda item: (str(item["category"]), str(item["id"])))
        bounded = facts[: max(1, min(int(limit), 50))]
        return {
            "memory_revision": context.memory_revision,
            "facts": bounded,
            "count": len(bounded),
            "has_more": len(facts) > len(bounded),
        }

    def get_planning_context(self, context: MainAgentContext) -> dict[str, Any]:
        brief = self.chat.briefs.active_for_conversation(
            context.user_id, context.conversation_id
        )
        runs = self.chat.runs.list(
            context.user_id, conversation_id=context.conversation_id, limit=20
        )
        explicit_itinerary = None
        if context.related_itinerary_id:
            try:
                explicit_itinerary = self.chat._owned_itinerary_summary(
                    context.user_id, context.related_itinerary_id
                )
            except OwnedResourceNotFound:
                explicit_itinerary = None
        return {
            "today": datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat(),
            "timezone": "Asia/Shanghai",
            "planning_brief": self.chat._brief_context(brief),
            "missing_fields": list((brief or {}).get("missing_fields") or []),
            "active_runs": [
                _public_run(item) for item in runs
                if item["status"] in {"queued", "running", "waiting_user"}
            ],
            "available_targets": self.chat._conversation_targets(
                context.user_id, context.conversation_id
            ),
            "explicit_target": {
                "run_id": context.related_run_id,
                "itinerary_id": context.related_itinerary_id,
                "itinerary": explicit_itinerary,
            },
        }

    def find_saved_itineraries(
        self,
        context: MainAgentContext,
        *,
        destination: str | None = None,
        duration_days: int | None = None,
        travel_start_from: str | None = None,
        travel_start_to: str | None = None,
        include_revisions: bool = False,
        limit: int = 5,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        bounded_limit = max(1, min(int(limit), 5))
        with itinerary_connection(self.db_path) as conn:
            rows = list_itinerary_records(context.user_id, conn)
        decoded = [self._itinerary_row(row) for row in rows]
        if not include_revisions:
            latest: dict[str, dict[str, Any]] = {}
            for item in decoded:
                root_id = item["root_id"]
                previous = latest.get(root_id)
                if previous is None or (item["version"], item["created_at"], item["itinerary_id"]) > (
                    previous["version"], previous["created_at"], previous["itinerary_id"]
                ):
                    latest[root_id] = item
            decoded = list(latest.values())
        decoded.sort(key=lambda item: (item["created_at"], item["itinerary_id"]), reverse=True)
        exact = [item for item in decoded if self._matches_itinerary(
            item, destination=destination, duration_days=duration_days,
            travel_start_from=travel_start_from, travel_start_to=travel_start_to,
        )]
        near = [item for item in decoded if item not in exact and self._near_itinerary(
            item, destination=destination, duration_days=duration_days,
        )]
        chosen = (exact or near)[:bounded_limit]
        match_kind = "exact" if exact else "near"
        artifact = None
        if chosen:
            artifact = ItineraryCollectionArtifact(
                title=(f"{destination}保存的方案" if destination else "保存的旅行方案"),
                match_kind=match_kind,
                items=[ItineraryCard.model_validate(item) for item in chosen],
            ).model_dump(mode="json")
        return ({
            "exact_matches": [self._model_itinerary(item) for item in exact[:bounded_limit]],
            "near_matches": [self._model_itinerary(item) for item in near[:bounded_limit]],
            "displayed_match_kind": match_kind if chosen else None,
            "count": len(chosen),
        }, artifact)

    def get_saved_itinerary(
        self, context: MainAgentContext, itinerary_id: str, day: int | None = None
    ) -> dict[str, Any]:
        with itinerary_connection(self.db_path) as conn:
            row = get_itinerary_record(itinerary_id, context.user_id, conn)
        if not row:
            raise OwnedResourceNotFound("itinerary not found")
        item = self._itinerary_row(row)
        plan = json.loads(row["plan_json"] or "{}")
        summary = self._model_itinerary(item)
        if day is None:
            return {"summary": summary, "highlights": item["highlights"]}
        if day < 1:
            raise ValueError("day must be at least 1")
        days = plan.get("days") or plan.get("itinerary") or []
        selected = next((entry for index, entry in enumerate(days, 1) if int(entry.get("day", index)) == day), None)
        if selected is None:
            raise ValueError("requested day not found")
        return {"summary": summary, "day": selected}

    async def update_current_brief(
        self, context: MainAgentContext, patch: dict[str, Any]
    ) -> AgentToolResult:
        run = self._chat_run(context)
        brief, error = await self.chat.actions.apply_brief_patch(run, patch)
        return AgentToolResult(
            ok=brief is not None, code="ok" if brief else "brief_validation_failed",
            message=error or "旅行需求已更新。",
            data={"brief": self.chat._brief_context(brief)} if brief else {},
        )

    async def submit_current_brief(self, context: MainAgentContext) -> AgentToolResult:
        run = self._chat_run(context)
        brief = self.chat.briefs.active_for_conversation(
            context.user_id, context.conversation_id
        )
        if not brief:
            brief = self.chat.briefs.latest_for_conversation(
                context.user_id, context.conversation_id
            )
        if brief and brief.get("status") == "submitted" and run.get("result_itinerary_id"):
            itinerary_id = str(run["result_itinerary_id"])
            with itinerary_connection(self.db_path) as conn:
                saved = get_itinerary_record(itinerary_id, context.user_id, conn)
            saved_state = json.loads((saved or {}).get("planner_state_json") or "{}")
            repair_summary = list(saved_state.get("walking_repair_summary") or [])
            message = "完整行程已经生成。"
            if repair_summary:
                message += "为避开交通冲突，已调整：" + "；".join(repair_summary) + "。"
            return AgentToolResult(
                ok=True, message=message,
                data={"brief_id": brief["id"], "itinerary_id": itinerary_id,
                      "walking_repair_summary": repair_summary},
            )
        missing = required_brief_fields((brief or {}).get("data") or {})
        if missing:
            try:
                answer = interrupt(required_input_interrupt(missing))
            except RuntimeError:
                return AgentToolResult(
                    ok=False, code="brief_not_ready",
                    message="当前需求还缺少必要信息。",
                    data={"missing_fields": missing},
                )
            return AgentToolResult(
                ok=False,
                code="required_input_received",
                message="用户补充了必要信息。请解析后更新当前 Brief，并再次提交；如果仍缺字段，继续收集。",
                data={"answer": answer, "previous_missing_fields": missing},
            )
        if not brief:
            return AgentToolResult(ok=False, code="brief_not_ready", message="当前还没有可提交的旅行需求。")
        coverage = brief.get("constraint_coverage") or []
        unsupported = [item for item in coverage if item.get("status") == "unsupported"]
        if unsupported:
            labels = {
                "accessibility_need": "\u65e0\u969c\u788d\u8981\u6c42",
                "dietary_requirement": "\u996e\u98df\u8981\u6c42",
                "transport_preference": "\u4ea4\u901a\u65b9\u5f0f\u8981\u6c42",
                "budget_style": "\u603b\u4ef7\u9884\u7b97\u8981\u6c42",
                "accommodation_preference": "\u4f4f\u5bbf\u786c\u6027\u8981\u6c42",
            }
            categories = {item.get("category") for item in unsupported}
            label_text = "\u3001".join(labels[key] for key in labels if key in categories)
            if not label_text:
                label_text = "\u90e8\u5206\u786c\u6027\u8981\u6c42"
            return AgentToolResult(
                ok=False,
                code="unsupported_constraint",
                message=(
                    f"\u5f53\u524d\u89c4\u5212\u6d41\u7a0b\u65e0\u6cd5\u4fdd\u8bc1\u6838\u9a8c{label_text}\uff0c"
                    "\u56e0\u6b64\u4e0d\u4f1a\u628a\u5b83\u4f5c\u4e3a\u5df2\u6ee1\u8db3\u6761\u4ef6\u751f\u6210\u884c\u7a0b\u3002"
                    "\u8bf7\u8c03\u6574\u4e3a\u504f\u597d\uff0c\u6216\u8865\u5145\u53ef\u6838\u9a8c\u7684\u6570\u636e\u540e\u518d\u786e\u8ba4\u3002"
                ),
                data={"constraint_coverage": coverage},
            )
        try:
            submitted, itinerary_id = await self.chat.execute_planner_tool(run, brief["id"])
        except Exception as exc:
            from app.planning.nodes import (
                CandidateShortfallPlanningError,
                UnsupportedPlanningConstraintError,
                WalkingTimePlanningError,
            )
            from app.planning.revision_timeout import PlanningExecutionTimeout
            if isinstance(exc, PlanningExecutionTimeout):
                return AgentToolResult(
                    ok=False, code=exc.public_code, message=exc.public_message,
                    data={"retryable": exc.public_retryable},
                )
            if isinstance(exc, CandidateShortfallPlanningError):
                from datetime import date, timedelta
                start = exc.start_date
                suggested_start = start.isoformat() if start else None
                suggested_end = (
                    (start + timedelta(days=exc.suggested_days - 1)).isoformat()
                    if start else None
                )
                return AgentToolResult(
                    ok=False, code="candidate_shortfall",
                    message=(
                        f"当前已核验候选不足以排满{exc.days}天；可考虑缩短到"
                        f"{exc.suggested_days}天（{suggested_start or '起始日期不变'}至"
                        f"{suggested_end or '结束日期待确认'}）。确认新的天数和日期后，我再重新规划。"
                    ),
                    data={
                        "current_days": exc.days,
                        "suggested_days": exc.suggested_days,
                        "suggested_start_date": suggested_start,
                        "suggested_end_date": suggested_end,
                        "requires_user_confirmation": True,
                    },
                )
            if isinstance(exc, WalkingTimePlanningError):
                status = str(exc.verification.get("status") or "unverified")
                transport = exc.verification.get("kind") == "transport"
                if status == "repair_limit_exhausted":
                    message = "已达到3轮自动修复上限，步行冲突仍未解决；本次没有生成行程，原方案保持不变。"
                elif status == "repair_no_feasible_solution":
                    message = "按实测步行时长重新求解后，当前硬约束下没有可行替换方案；本次没有生成行程，原方案保持不变。"
                elif status == "budget_exhausted":
                    message = "本次规划的步行查询预算已用完，仍有路段未核验；这不代表路线无解，本次没有保存行程。"
                elif status == "conflict":
                    message = "真实步行时长与开放时间、预约或每日边界仍有冲突，本次没有生成行程。"
                else:
                    message = "真实步行时长暂时无法核验，本次没有生成行程。请稍后重试或调整安排。"
                return AgentToolResult(
                    ok=False,
                    code=f"{'transport_time' if transport else 'walking_time'}_{status}",
                    message=message.replace("步行", "交通").replace("实测", "预计").replace("真实", "预计").replace("当前硬约束下没有可行替换方案", "本次有界搜索未找到可行替换方案") if transport else message,
                    data={
                        "walking_time_verification": exc.verification,
                        "failure_snapshot": exc.failure_snapshot,
                    },
                )
            if isinstance(exc, UnsupportedPlanningConstraintError):
                return AgentToolResult(
                    ok=False,
                    code="unsupported_constraint",
                    message=(
                        "\u5f53\u524d\u65c5\u884c\u7ea6\u675f\u4e0d\u5728\u6743\u5a01\u5730\u70b9\u6216\u89c4\u5212\u6570\u636e\u4e2d\u53ef\u6838\u9a8c\uff0c"
                        "\u672c\u6b21\u6ca1\u6709\u751f\u6210\u884c\u7a0b\u3002\u8bf7\u786e\u8ba4\u5730\u70b9\u540d\u6216\u653e\u5bbd\u8be5\u6761\u8981\u6c42\u540e\u91cd\u8bd5\u3002"
                    ),
                    data={"constraint_coverage": exc.coverage},
                )
            raise
        repair_summary = list(submitted.get("walking_repair_summary") or [])
        message = "完整行程已经生成。"
        if repair_summary:
            message += "为避开交通冲突，已调整：" + "；".join(repair_summary) + "。"
        return AgentToolResult(
            ok=True, message=message,
            data={
                "brief_id": submitted["id"], "itinerary_id": itinerary_id,
                "walking_repair_summary": repair_summary,
            },
        )

    async def start_revision(
        self, context: MainAgentContext, itinerary_id: str, modification_notes: str, *, transport_budget: str | None = None
    ) -> AgentToolResult:
        run = self._chat_run(context)
        bound = run["request_snapshot"].get("related_itinerary_id")
        if bound and bound != itinerary_id:
            return AgentToolResult(
                ok=False, code="revision_target_mismatch",
                message="这条消息已绑定另一份行程，请在对应行程中继续修改。",
            )
        from app.planning.full_revision import (
            FullRevisionIntentError, is_full_revision_request,
            parse_confirmed_full_revision,
        )
        from app.planning.local_reorder import (
            LocalReorderRejected, extract_explicit_target_day, is_explicit_order_request,
        )
        from app.planning.local_add_remove import (
            is_add_remove_request, is_replace_request, parse_day_set_change,
            parse_day_replacement,
        )
        # The server-injected current instruction outranks a model's paraphrase.
        # Send it whole through the same strict parsers: this also prevents the
        # model from silently discarding an unsupported clause. Contextual
        # confirmations without an operation still use validated tool notes.
        current = context.current_message.strip()
        if transport_budget is not None and not is_full_revision_request(modification_notes):
            return AgentToolResult(ok=False, code="revision_scope_confirmation_required",
                                   message="更改全程交通预算需要明确全量修改；本次没有修改原行程。")
        if any(recognize(current) for recognize in (
            is_full_revision_request, is_replace_request,
            is_add_remove_request, is_explicit_order_request,
        )):
            modification_notes = current
        if is_full_revision_request(modification_notes):
            try:
                intent = parse_confirmed_full_revision(modification_notes)
                result_id, verification = await self.chat.execute_full_revision_tool(
                    run, itinerary_id, modification_notes, intent,
                    **({"transport_budget": transport_budget} if transport_budget is not None else {}),
                )
            except LocalReorderRejected as exc:
                return AgentToolResult(ok=False, code=exc.code, message=exc.public_message, data=exc.data)
            except FullRevisionIntentError as exc:
                return AgentToolResult(ok=False, code=exc.code, message=exc.public_message)
            except Exception as exc:
                from app.planning.revision_timeout import RevisionTimeoutError
                if isinstance(exc, RevisionTimeoutError):
                    raise
                from app.planning.revision_timeout import PlanningExecutionTimeout
                if isinstance(exc, PlanningExecutionTimeout):
                    return AgentToolResult(
                        ok=False, code=exc.public_code,
                        message="全量修改与交通冲突修复超过3分钟，原行程保持不变；请稍后重试或调整安排。",
                        data={"retryable": exc.public_retryable},
                    )
                from app.planning.nodes import (
                    CandidateShortfallPlanningError,
                    PlanningQualityError, UnsupportedPlanningConstraintError,
                    WalkingTimePlanningError,
                )
                from app.core.memory import ItineraryVersionConflict
                if isinstance(exc, WalkingTimePlanningError):
                    verification = exc.verification
                    transport = verification.get("kind") == "transport"
                    if verification.get("status") == "budget_exhausted":
                        message = "步行查询预算已用完，仍有路段未核验，不能据此判断路线无解；原行程保持不变。"
                    elif verification.get("status") == "repair_limit_exhausted":
                        message = "达到3轮自动修复上限后仍有真实步行冲突，原行程保持不变。"
                    elif verification.get("status") == "repair_no_feasible_solution":
                        message = "按实测步行时长重新求解后，当前硬约束下没有可行替换方案；原行程保持不变。"
                    else:
                        message = "全量修改的真实步行时间未能通过核验，原行程保持不变。"
                    return AgentToolResult(
                        ok=False, code=f"{'transport_time' if transport else 'walking_time'}_{verification.get('status', 'unverified')}",
                        message=message.replace("步行", "交通").replace("实测", "预计").replace("真实", "预计").replace("当前硬约束下没有可行替换方案", "本次有界搜索未找到可行替换方案") if transport else message,
                        data={
                            "walking_time_verification": verification,
                            "failure_snapshot": exc.failure_snapshot,
                        },
                    )
                if isinstance(exc, UnsupportedPlanningConstraintError):
                    return AgentToolResult(
                        ok=False, code="unsupported_constraint",
                        message="至少一项已确认硬约束无法由权威地点和规划数据核验，原行程保持不变。",
                        data={"constraint_coverage": exc.coverage},
                    )
                if isinstance(exc, CandidateShortfallPlanningError):
                    from datetime import timedelta
                    start = exc.start_date
                    start_text = start.isoformat() if start else "起始日期不变"
                    end_text = (
                        (start + timedelta(days=exc.suggested_days - 1)).isoformat()
                        if start else "结束日期待确认"
                    )
                    return AgentToolResult(
                        ok=False, code="candidate_shortfall",
                        message=(
                            f"当前已核验候选不足以排满{exc.days}天；可考虑缩短到{exc.suggested_days}天"
                            f"（{start_text}至{end_text}）。请确认新的天数和日期后，我再重排。原行程保持不变。"
                        ),
                        data={
                            "current_days": exc.days,
                            "suggested_days": exc.suggested_days,
                            "suggested_start_date": start.isoformat() if start else None,
                            "suggested_end_date": end_text if start else None,
                            "requires_user_confirmation": True,
                        },
                    )
                if isinstance(exc, PlanningQualityError):
                    return AgentToolResult(
                        ok=False, code="revision_quality_rejected",
                        message="全量修改未通过独立质量校验，原行程保持不变。",
                        data={"reason": str(exc)},
                    )
                if isinstance(exc, ItineraryVersionConflict):
                    return AgentToolResult(
                        ok=False, code="revision_conflict",
                        message="基础行程版本已变化，请刷新后重试；本次没有保存新版本。",
                    )
                if isinstance(exc, ValueError) and str(exc) in {
                    "revision_base_unverified", "revision_date_range_unsupported",
                }:
                    return AgentToolResult(
                        ok=False, code=str(exc),
                        message="基础行程缺少可复用的已确认规划数据或日期范围不受支持，原版本保持不变。",
                    )
                import sqlite3
                from sqlalchemy.exc import DBAPIError
                if isinstance(exc, (sqlite3.DatabaseError, DBAPIError)):
                    return AgentToolResult(
                        ok=False, code="revision_system_error",
                        message="全量修改的存储操作暂时失败，原行程保持不变；请稍后重试。",
                    )
                return AgentToolResult(
                    ok=False, code="revision_system_error",
                    message="全量修改执行失败，原行程保持不变；请稍后重试。",
                )
            repair_summary = list(verification.get("repair_summary") or [])
            message = "全量行程已通过约束、质量和交通预计时间核验，并保存为关联新版本。"
            if repair_summary:
                message += "为避开交通冲突，已调整：" + "；".join(repair_summary) + "。"
            return AgentToolResult(
                ok=True, message=message,
                data={"itinerary_id": result_id, "walking_time_verification": verification,
                      "revision_scope": "full_revision"},
            )

        if is_replace_request(modification_notes):
            try:
                target_day, _operation = parse_day_replacement(modification_notes)
                result_id, verification = await self.chat.execute_day_replace_tool(
                    run, itinerary_id, target_day, modification_notes
                )
            except LocalReorderRejected as exc:
                return AgentToolResult(ok=False, code=exc.code, message=exc.public_message, data=exc.data)
            return AgentToolResult(ok=True, message="当天景点替换已核验并保存为新版本。", data={
                "itinerary_id": result_id, "target_day": target_day, "walking_time_verification": verification,
            })
        if is_add_remove_request(modification_notes):
            try:
                target_day, _operation = parse_day_set_change(modification_notes)
                result_id, verification = await self.chat.execute_day_set_change_tool(
                    run, itinerary_id, target_day, modification_notes
                )
            except LocalReorderRejected as exc:
                return AgentToolResult(ok=False, code=exc.code, message=exc.public_message, data=exc.data)
            return AgentToolResult(ok=True, message="当天景点变更已核验并保存为新版本。", data={
                "itinerary_id": result_id, "target_day": target_day, "walking_time_verification": verification,
            })
        if is_explicit_order_request(modification_notes):
            try:
                target_day = extract_explicit_target_day(modification_notes)
                result_id, verification = await self.chat.execute_day_reorder_tool(
                    run, itinerary_id, target_day, modification_notes
                )
            except LocalReorderRejected as exc:
                return AgentToolResult(
                    ok=False, code=exc.code, message=exc.public_message, data=exc.data,
                )
            return AgentToolResult(
                ok=True, message="当天路线已核验并保存为新版本。",
                data={
                    "itinerary_id": result_id, "target_day": target_day,
                    "walking_time_verification": verification,
                },
            )
        return AgentToolResult(
            ok=False, code="revision_intent_unresolved",
            message="请说明明确的单日修改，或确认全量修改范围并给出可验证的具体变更；原行程保持不变。",
        )

    async def reorder_itinerary_day(
        self,
        context: MainAgentContext,
        itinerary_id: str,
        target_day: int,
        modification_notes: str,
    ) -> AgentToolResult:
        run = self._chat_run(context)
        bound = run["request_snapshot"].get("related_itinerary_id")
        if bound and bound != itinerary_id:
            return AgentToolResult(
                ok=False, code="revision_target_mismatch",
                message="这条消息已绑定另一份行程，请在对应行程中继续修改。",
            )
        from app.planning.local_reorder import LocalReorderRejected
        try:
            result_id, verification = await self.chat.execute_day_reorder_tool(
                run, itinerary_id, target_day, modification_notes
            )
        except LocalReorderRejected as exc:
            return AgentToolResult(
                ok=False, code=exc.code, message=exc.public_message, data=exc.data,
            )
        return AgentToolResult(
            ok=True, message="当天路线已核验并保存为新版本。",
            data={
                "itinerary_id": result_id,
                "target_day": target_day,
                "walking_time_verification": verification,
            },
        )
    async def control_run(
        self, context: MainAgentContext, run_id: str, action: str, *, requires_confirmation: bool = False
    ) -> AgentToolResult:
        run = self._chat_run(context)
        controlled, error = await self.chat.actions.control_run(
            run, run_id, action, requires_confirmation=requires_confirmation
        )
        return AgentToolResult(
            ok=controlled is not None, code="ok" if controlled else "run_control_rejected",
            message=error or "任务状态已更新。",
            data={"run": _public_run(controlled)} if controlled else {},
        )

    def _chat_run(self, context: MainAgentContext) -> dict[str, Any]:
        run = self.chat.runs.get(context.user_id, context.chat_run_id)
        if run["kind"] != RunKind.CHAT.value or run["conversation_id"] != context.conversation_id:
            raise OwnedResourceNotFound("chat run not found")
        return run

    @staticmethod
    def _matches_itinerary(
        item: dict[str, Any], *, destination: str | None, duration_days: int | None,
        travel_start_from: str | None, travel_start_to: str | None,
    ) -> bool:
        if destination and _clean_text(item["destination"]) != _clean_text(destination):
            return False
        if duration_days is not None and item["duration_days"] != duration_days:
            return False
        if travel_start_from and str(item.get("start_date") or "") < travel_start_from:
            return False
        if travel_start_to and str(item.get("start_date") or "") > travel_start_to:
            return False
        return True

    @staticmethod
    def _near_itinerary(
        item: dict[str, Any], *, destination: str | None, duration_days: int | None
    ) -> bool:
        destination_match = not destination or _clean_text(destination) in _clean_text(item["destination"])
        duration_match = duration_days is None or (
            item["duration_days"] is not None and abs(item["duration_days"] - duration_days) <= 1
        )
        return destination_match and duration_match

    @classmethod
    def _itinerary_row(cls, row: Any) -> dict[str, Any]:
        plan = json.loads(row["plan_json"] or "{}")
        root_id = row["root_id"] or row["id"]
        version = int(row["version"] or 1)
        duration = _date_days(row["start_date"], row["end_date"])
        if duration is None:
            days = plan.get("days") or plan.get("itinerary") or []
            duration = len(days) or None
        return {
            "itinerary_id": row["id"], "root_id": root_id, "version": version,
            "destination": row["destination"] or plan.get("destination") or "",
            "duration_days": duration, "start_date": row["start_date"] or None,
            "end_date": row["end_date"] or None, "created_at": row["created_at"] or "",
            "is_modified": bool(row["parent_id"] or row["modification_notes"] or version > 1),
            "highlights": cls._highlights(plan),
        }

    @staticmethod
    def _highlights(plan: dict[str, Any]) -> list[str]:
        values: list[str] = []
        for day in plan.get("days") or plan.get("itinerary") or []:
            entries = day.get("activities") or day.get("stops") or day.get("items") or []
            for entry in entries:
                name = entry.get("name") or entry.get("title") or entry.get("attraction")
                if name and str(name) not in values:
                    values.append(str(name)[:60])
                if len(values) >= 4:
                    return values
        return values

    @staticmethod
    def _model_itinerary(item: dict[str, Any]) -> dict[str, Any]:
        return {
            key: item[key] for key in (
                "itinerary_id", "root_id", "version", "destination",
                "duration_days", "start_date", "end_date", "is_modified", "highlights",
            )
        }

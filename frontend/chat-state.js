// chat-state.js — Chat 页面可测试的实体化状态 reducer

(function (global) {
  const initialState = () => ({
    messages: {},
    messageOrder: [],
    briefs: {},
    currentBriefId: null,
    runs: {},
    agentActivities: {},
    cursors: {},
  });

  const PRODUCT_STAGES = [
    { key: "understand", label: "理解旅行需求" },
    { key: "discover", label: "搜集目的地信息" },
    { key: "compose", label: "编排行程与优化路线" },
    { key: "polish", label: "完善旅行细节" },
  ];

  const INTERNAL_STAGE_MAP = {
    weather_lookup: "understand",
    intent: "understand",
    query_rewrite: "understand",
    attraction_search: "discover",
    candidate_builder: "discover",
    optimizer: "compose",
    quality_gate: "compose",
    planner: "compose",
    reviewer: "compose",
    time_check: "compose",
    meal_search: "polish",
    meal_recommend: "polish",
    spot_tips: "polish",
    finalize: "polish",
  };
  const JOURNEY_STAGE_INDEX = {
    weather_lookup: 0,
    intent: 0,
    query_rewrite: 0,
    attraction_search: 1,
    candidate_builder: 1,
    optimizer: 2,
    quality_gate: 2,
    planner: 2,
    reviewer: 3,
    time_check: 4,
    meal_search: 5,
    meal_recommend: 5,
    spot_tips: 5,
    finalize: 5,
  };

  function planningBriefStatusLabel(status) {
    return {
      collecting: "信息收集中",
      ready: "等待确认",
      submitted: "已提交",
      discarded: "已放弃",
    }[status] || status;
  }

  function conversationAttention(conversation) {
    if (!conversation) return null;
    if (conversation.status === "archived") {
      return { kind: "archived", label: "已归档", ariaLabel: "对话已归档" };
    }
    if (conversation.has_waiting_user) {
      return { kind: "waiting-user", label: "待你回复", ariaLabel: "规划正在等待你的回复" };
    }
    if (conversation.has_ready_brief) {
      return { kind: "ready-brief", label: "待确认", ariaLabel: "旅行方案等待你确认" };
    }
    if (conversation.has_active_planning) {
      return { kind: "planning", label: "规划中", ariaLabel: "旅行行程正在规划" };
    }
    if (conversation.has_unread_completed) {
      return { kind: "unread", label: "新行程", ariaLabel: "有尚未查看的新行程" };
    }
    return null;
  }

  function shouldMarkConversationViewed(visibilityState, activeConversationId, conversationId) {
    return visibilityState === "visible"
      && !!activeConversationId
      && activeConversationId === conversationId;
  }

  function shouldPollConversations(visibilityState) {
    return visibilityState === "visible";
  }

  function upsertMessage(state, message) {
    const normalized = {
      ...message,
      artifacts: Array.isArray(message.artifacts) ? message.artifacts.slice(0, 5) : [],
    };
    const exists = !!state.messages[message.id];
    return {
      ...state,
      messages: { ...state.messages, [message.id]: { ...state.messages[message.id], ...normalized } },
      messageOrder: exists
        ? state.messageOrder
        : [...state.messageOrder, message.id].sort((a, b) => {
            const left = state.messages[a]?.sequence ?? message.sequence ?? Number.MAX_SAFE_INTEGER;
            const right = state.messages[b]?.sequence ?? message.sequence ?? Number.MAX_SAFE_INTEGER;
            return left - right;
          }),
    };
  }

  function productStageFor(stage) {
    return INTERNAL_STAGE_MAP[stage] || null;
  }

  function interactionInputKind(interaction) {
    const schema = interaction?.input_schema || {};
    const question = String(interaction?.question || "");
    if (schema.format === "date-range" || /日期|开始.*结束|start_date|end_date/.test(question)) {
      return "date-range";
    }
    if (Array.isArray(schema.enum)) return "single-choice";
    if (schema.type === "array" && Array.isArray(schema.items?.enum)) return "multi-choice";
    return "text";
  }

  function constraintPresentation(fact) {
    const contextOnly = fact?.application_level === "context_only";
    const polarity = contextOnly ? "fact" : (fact?.polarity || "fact");
    return {
      prefer: {
        tone: "prefer", badge: "优先考虑", summaryLabel: "偏好",
        effect: "规划时会优先考虑", excludeAction: "本次不优先", restoreAction: "恢复优先",
      },
      avoid: {
        tone: "avoid", badge: "本次避开", summaryLabel: "避开",
        effect: "规划时将排除，不纳入候选行程", excludeAction: "本次允许安排", restoreAction: "恢复避开",
      },
      require: {
        tone: "require", badge: "必须满足", summaryLabel: "必须",
        effect: "将作为本次行程的硬性要求", excludeAction: "本次取消要求", restoreAction: "恢复要求",
      },
      fact: {
        tone: "context", badge: "仅作背景", summaryLabel: "背景",
        effect: "只用于理解行程，不代表要安排", excludeAction: "本次不参考", restoreAction: "恢复参考",
      },
    }[polarity] || {
      tone: "context", badge: "仅作背景", summaryLabel: "背景",
      effect: "只用于理解行程，不代表要安排", excludeAction: "本次不参考", restoreAction: "恢复参考",
    };
  }

  function briefViewModel(brief) {
    const data = brief?.data || {};
    const missingLabels = {
      destination: "目的地",
      start_date: "开始日期",
      end_date: "结束日期",
      date_range: "有效的日期范围",
      dates_or_days: "具体出行日期",
    };
    const categoryLabels = {
      attraction_preference: "景点", food_preference: "餐饮", dietary_requirement: "饮食要求",
      travel_pace: "旅行节奏", budget_style: "预算习惯", transport_preference: "交通",
      accommodation_preference: "住宿", schedule_preference: "作息", companion_context: "同行",
      accessibility_need: "无障碍", other_travel_preference: "其他",
    };
    const preferences = (data.trip_constraints || []).map(item => {
      const presentation = constraintPresentation(item);
      return [
        `${categoryLabels[item.category] || "旅行要求"} · ${presentation.summaryLabel}`,
        item.value_text,
      ];
    });
    [["景点", data.attraction_preference], ["餐饮", data.food_preference], ["旅行节奏", data.habit_preference]]
      .forEach(([label, value]) => {
        if (value && !preferences.some(([, existing]) => existing === value)) preferences.push([label, value]);
      });
    if (data.trip_budget || data.budget) preferences.unshift(["本次预算", data.trip_budget || data.budget]);
    return {
      destination: data.destination || "还没决定",
      dateLabel: data.start_date && data.end_date
        ? `${data.start_date} — ${data.end_date}`
        : data.days ? `${data.days} 天 · 日期待定` : "日期待补充",
      preferences,
      usesDefaults: brief?.status === "ready"
        && !(data.trip_constraints || []).length,
      missing: (brief?.missing_fields || []).map(field => missingLabels[field] || field),
    };
  }

  function runStatusText(run) {
    if (run?.status === "waiting_user") {
      return run.pending_interaction?.question
        || run.pending_interaction?.prompt
        || "请在下方补充信息，继续当前任务。";
    }
    if (run?.status === "queued") return "正在准备处理你的问题…";
    const activity = run?.agent_activity;
    return run?.latest_progress_label
      || (["started", "progress"].includes(activity?.status) ? activity.label : "")
      || run?.stage_label
      || "正在理解你的问题并整理回复…";
  }

  function advanceRunStage(run, internalStage, label) {
    const key = productStageFor(internalStage);
    if (!key) return { ...run, latest_progress_label: label || run.latest_progress_label };
    const incomingIndex = PRODUCT_STAGES.findIndex(stage => stage.key === key);
    const currentIndex = PRODUCT_STAGES.findIndex(stage => stage.key === run.product_stage);
    const nextIndex = Math.max(incomingIndex, currentIndex, 0);
    const incomingJourneyIndex = JOURNEY_STAGE_INDEX[internalStage];
    const currentJourneyIndex = Number.isInteger(run.journey_step_index)
      ? run.journey_step_index
      : -1;
    return {
      ...run,
      product_stage: PRODUCT_STAGES[nextIndex].key,
      completed_product_stages: PRODUCT_STAGES
        .slice(0, nextIndex)
        .map(stage => stage.key),
      journey_step_index: Number.isInteger(incomingJourneyIndex)
        ? Math.max(incomingJourneyIndex, currentJourneyIndex)
        : currentJourneyIndex,
      internal_stage: Number.isInteger(incomingJourneyIndex)
        && incomingJourneyIndex >= currentJourneyIndex
        ? internalStage
        : run.internal_stage,
      latest_progress_label: label || run.latest_progress_label,
    };
  }

  function activityItems(state) {
    const items = [];
    state.messageOrder.forEach((id) => {
      const entity = state.messages[id];
      if (entity) items.push({
        key: `message:${id}`,
        type: "message",
        entityId: id,
        entity,
        createdAt: entity.created_at || "",
        sequence: Number(entity.sequence || Number.MAX_SAFE_INTEGER),
      });
    });
    Object.values(state.briefs).forEach((entity) => {
      // Collecting requirements are handled conversationally.  Only a ready
      // brief gets a compact, actionable confirmation surface in the feed.
      if (!entity || entity.status !== "ready") return;
      items.push({
        key: `brief:${entity.id}`,
        type: "brief",
        entityId: entity.id,
        entity,
        createdAt: entity.created_at || entity.updated_at || "",
        sequence: Number.MAX_SAFE_INTEGER,
      });
    });
    Object.values(state.runs).forEach((entity) => {
      if (!entity) return;
      // Tips enrich an already created itinerary in the background.  They are
      // intentionally silent in the conversation and never get their own
      // progress card or timeline entry.
      if (entity.kind === "spot_tips") return;
      if (entity.kind === "chat") {
        if (["queued", "running"].includes(entity.status)) {
          items.push({
            key: entity.journey_step_index !== undefined ? `run:${entity.id}` : `chat-thinking:${entity.id}`,
            type: entity.journey_step_index !== undefined ? "run" : "chat_thinking",
            entityId: entity.id,
            entity,
            createdAt: entity.activity_at || entity.updated_at || entity.created_at || entity.queued_at || "",
            sequence: Number.MAX_SAFE_INTEGER,
          });
          return;
        }
        if (entity.status === "waiting_user") {
          items.push({
            key: `run:${entity.id}`,
            type: "run",
            entityId: entity.id,
            entity,
            createdAt: entity.activity_at || entity.updated_at || entity.created_at || entity.queued_at || "",
            sequence: Number.MAX_SAFE_INTEGER,
          });
          return;
        }
        if (entity.status === "succeeded" && entity.result_itinerary_id) {
          items.push({
            key: `run:${entity.id}`, type: "run", entityId: entity.id, entity,
            createdAt: entity.activity_at || entity.updated_at || entity.created_at || "",
            sequence: Number.MAX_SAFE_INTEGER,
          });
          return;
        }
        if (entity.status !== "failed" && !(entity.status === "cancelled" && entity.journey_step_index !== undefined)) return;
        if (entity.journey_step_index !== undefined) {
          items.push({
            key: `run:${entity.id}`,
            type: "run",
            entityId: entity.id,
            entity,
            createdAt: entity.activity_at || entity.updated_at || entity.created_at || "",
            sequence: Number.MAX_SAFE_INTEGER,
          });
          return;
        }
        items.push({
          key: `chat-failure:${entity.id}`,
          type: "chat_failure",
          entityId: entity.id,
          entity,
          createdAt: entity.created_at || entity.queued_at || entity.updated_at || "",
          sequence: Number.MAX_SAFE_INTEGER,
        });
        return;
      }
      // Planner and Revision are internal task tools of the Chat Run.  Ignore
      // legacy child runs so old persisted data cannot resurrect card UI.
      return;
    });
    const typePriority = { message: 0, chat_thinking: 1, brief: 2, run: 3, chat_failure: 4 };
    return items.sort((left, right) => {
      if (left.type === "run" && right.type === "message"
          && right.entity.role === "assistant" && right.entity.related_run_id === left.entity.id) return -1;
      if (right.type === "run" && left.type === "message"
          && left.entity.role === "assistant" && left.entity.related_run_id === right.entity.id) return 1;
      if (left.type === "message" && right.type === "message" && left.sequence !== right.sequence) {
        return left.sequence - right.sequence;
      }
      if (left.createdAt && right.createdAt && left.createdAt !== right.createdAt) {
        return left.createdAt.localeCompare(right.createdAt);
      }
      if (left.createdAt !== right.createdAt) return left.createdAt ? -1 : 1;
      if (left.sequence !== right.sequence) return left.sequence - right.sequence;
      if (typePriority[left.type] !== typePriority[right.type]) {
        return typePriority[left.type] - typePriority[right.type];
      }
      return left.key.localeCompare(right.key);
    });
  }

  function applyEvent(state, runId, event) {
    const seq = Number(event.sequence || 0);
    if (seq && seq <= Number(state.cursors[runId] || 0)) return state;
    let next = seq
      ? { ...state, cursors: { ...state.cursors, [runId]: seq } }
      : state;
    const payload = event.payload || {};
    if (event.kind === "messages") {
      const id = payload.message_id || `assistant:${runId}`;
      const current = next.messages[id] || {
        id, role: "assistant", content: "", streaming: true, related_run_id: runId,
      };
      next = upsertMessage(next, {
        ...current,
        content: current.content + (payload.delta || ""),
      });
    } else if (event.kind === "custom") {
      if (payload.kind === "chat.message.completed") {
        const tempId = `assistant:${runId}`;
        const withoutTemp = { ...next.messages };
        delete withoutTemp[tempId];
        next = {
          ...next,
          messages: withoutTemp,
          messageOrder: next.messageOrder.filter(id => id !== tempId),
        };
        next = upsertMessage(next, {
          id: payload.message_id,
          role: "assistant",
          content: payload.content,
          artifacts: payload.artifacts || [],
          sequence: payload.sequence,
          created_at: payload.created_at,
          related_run_id: runId,
          related_itinerary_id: payload.related_itinerary_id || null,
          streaming: false,
        });
      } else if (String(payload.kind || "").startsWith("agent.activity.")) {
        const activityId = payload.activity_id;
        if (activityId) {
          const status = payload.kind.split(".").pop();
          const activity = {
            ...next.agentActivities[activityId], ...payload,
            id: activityId, run_id: runId, status,
          };
          next = {
            ...next,
            agentActivities: { ...next.agentActivities, [activityId]: activity },
            runs: {
              ...next.runs,
              [runId]: { ...next.runs[runId], agent_activity: activity },
            },
          };
        }
      } else if (String(payload.kind || "").startsWith("planning_brief.")) {
        const stored = next.briefs[payload.brief_id];
        if (stored?.updated_at && event.created_at
          && Date.parse(event.created_at) < Date.parse(stored.updated_at)) return next;
        const current = next.briefs[next.currentBriefId];
        const older = current && event.created_at && current.updated_at
          && Date.parse(event.created_at) < Date.parse(current.updated_at);
        next = {
          ...next,
          currentBriefId: older ? next.currentBriefId : payload.status === "discarded"
            ? (next.currentBriefId === payload.brief_id ? null : next.currentBriefId) : payload.brief_id,
          briefs: {
            ...next.briefs,
            [payload.brief_id]: {
              ...next.briefs[payload.brief_id],
              id: payload.brief_id,
              status: payload.status,
              data: payload.summary || {},
              missing_fields: payload.missing_fields || [],
              updated_at: event.created_at || next.briefs[payload.brief_id]?.updated_at,
            },
          },
        };
      } else if (payload.kind === "run.created") {
        // Kept as a no-op for backward-compatible event streams. New planner
        // and revision work is emitted as internal progress on this Chat Run.
      } else {
        const currentRun = next.runs[runId] || {};
        let pendingInteraction = currentRun.pending_interaction;
        if (payload.kind === "run.waiting_user") {
          pendingInteraction = payload;
        } else if (
          payload.kind === "run.status"
          && payload.status !== "waiting_user"
        ) {
          pendingInteraction = null;
        }
        const stagedRun = payload.kind === "planning_run.progress"
          ? advanceRunStage(currentRun, payload.stage, payload.label)
          : currentRun;
        const itineraryResult = payload.kind === "planning.itinerary_created"
          ? {
              result_itinerary_id: payload.itinerary_id,
              request_snapshot: {
                ...(currentRun.request_snapshot || {}),
                ...(payload.destination && !currentRun.request_snapshot?.destination
                  ? { destination: payload.destination }
                  : {}),
              },
            }
          : {};
        next = {
          ...next,
          runs: {
            ...next.runs,
            [runId]: {
              ...stagedRun,
              ...itineraryResult,
              ...(payload.kind === "run.status" ? { status: payload.status } : {}),
              ...(["run.waiting_user", "run.status"].includes(payload.kind)
                ? { activity_at: event.created_at || new Date().toISOString() } : {}),
              pending_interaction: pendingInteraction,
              last_event: payload,
            },
          },
        };
        if (payload.kind === "run.status" && ["failed", "cancelled"].includes(payload.status)) {
          const tempId = `assistant:${runId}`;
          if (next.messages[tempId]) {
            const messages = { ...next.messages };
            delete messages[tempId];
            next = {
              ...next,
              messages,
              messageOrder: next.messageOrder.filter(id => id !== tempId),
            };
          }
        }
      }
    } else if (event.kind === "end") {
      next = {
        ...next,
        runs: {
          ...next.runs,
          [runId]: { ...next.runs[runId], status: payload.status || next.runs[runId]?.status },
        },
      };
    }
    return next;
  }

  global.ChatState = {
    initialState,
    upsertMessage,
    applyEvent,
    activityItems,
    advanceRunStage,
    runStatusText,
    productStageFor,
    interactionInputKind,
    constraintPresentation,
    briefViewModel,
    PRODUCT_STAGES,
    planningBriefStatusLabel,
    conversationAttention,
    shouldMarkConversationViewed,
    shouldPollConversations,
  };
})(typeof window === "undefined" ? globalThis : window);

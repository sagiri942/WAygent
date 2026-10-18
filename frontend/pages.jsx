// pages.jsx — 三个页面 + Auth 模态框

/* ── Auth 模态框 ──────────────────────────────── */
function AuthModal({ onSuccess, onClose, reason }) {
  const [tab, setTab] = React.useState("login");
  const [username, setUsername] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [err, setErr] = React.useState("");
  const [loading, setLoading] = React.useState(false);

  const submit = async () => {
    if (!username.trim() || !password.trim()) { setErr("请填写用户名和密码"); return; }
    setLoading(true); setErr("");
    try {
      let res;
      if (tab === "login") {
        res = await loginApi(username.trim(), password);
      } else {
        res = await registerApi(username.trim(), password);
        if (res.token) {
          // 注册成功后直接登录
        } else {
          // 注册成功但需要再登录
          res = await loginApi(username.trim(), password);
        }
      }
      setAuth(res.token, res.username || username.trim());
      onSuccess && onSuccess(res.username || username.trim());
    } catch (e) {
      setErr(e.message || "操作失败");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="modal-backdrop" onClick={(e) => e.target === e.currentTarget && onClose && onClose()}>
      <div className="modal-card">
        <button className="modal-close" onClick={onClose} aria-label="关闭登录窗口"><UiIcon name="close" size={18} /></button>
        <div className="modal-title">WAygent · AI 旅行规划</div>
        {reason && <div className="modal-sub">{reason}</div>}
        <div className="auth-tabs">
          <button className={`auth-tab ${tab === "login" ? "active" : ""}`} onClick={() => { setTab("login"); setErr(""); }}>登录</button>
          <button className={`auth-tab ${tab === "register" ? "active" : ""}`} onClick={() => { setTab("register"); setErr(""); }}>注册</button>
        </div>
        <div className="form-field">
          <label className="form-label">用户名</label>
          <input className="form-input" value={username} onChange={e => setUsername(e.target.value)}
            placeholder="输入用户名" autoFocus
            onKeyDown={e => e.key === "Enter" && submit()} />
        </div>
        <div className="form-field">
          <label className="form-label">密码</label>
          <input className="form-input" type="password" value={password} onChange={e => setPassword(e.target.value)}
            placeholder="输入密码"
            onKeyDown={e => e.key === "Enter" && submit()} />
        </div>
        <div className="form-error">{err}</div>
        <button className="form-submit" disabled={loading} onClick={submit}>
          {loading ? "处理中…" : tab === "login" ? "登录" : "注册并登录"}
        </button>
      </div>
    </div>
  );
}

/* ── 持久化旅行对话 ─────────────────────────────── */
// 会话关联只来自持久化 ID；检索消息里的多方案推荐不代表当前行程。
function latestConversationItinerary(messages, runs) {
  const associations = [
    ...messages.filter(item => item.related_itinerary_id).map(item => ({
      id: item.related_itinerary_id, time: item.created_at, sequence: item.sequence || 0,
    })),
    ...runs.filter(item => item.result_itinerary_id).map(item => ({
      id: item.result_itinerary_id, time: item.updated_at || item.created_at, sequence: 0,
    })),
  ];
  associations.sort((a, b) => (Date.parse(b.time) || 0) - (Date.parse(a.time) || 0) || b.sequence - a.sequence);
  return associations[0]?.id || null;
}

function ConversationCover({ conversation, currentUsername, planId, associationError = false }) {
  const coverRef = React.useRef(null);
  const [cover, setCover] = React.useState({ photo: null, label: "暂无行程封面" });
  const versionId = planId;
  const placeholder = associationError ? "封面暂不可用" : "暂无行程封面";
  const identity = JSON.stringify([currentUsername, conversation.id, conversation.updated_at, versionId, associationError]);
  const visibleCover = cover.identity === identity ? cover : { photo:null, label:placeholder };
  React.useEffect(() => {
    let cancelled = false;
    setCover({ identity, photo: null, label: placeholder });
    const node = coverRef.current;
    if (!node) return undefined;
    const load = async () => {
      try {
        let id = versionId;
        if (id === undefined) {
          const [messages, runs] = await Promise.all([
            getConversationMessages(conversation.id), listRuns(conversation.id),
          ]);
          if (cancelled) return;
          id = latestConversationItinerary(messages, runs);
        }
        if (!id || cancelled) return;
        const data = await getHistoryItem(id);
        if (!cancelled) setCover({ identity, photo: firstDayFirstPoiPhoto(data), label: "暂无行程封面" });
      } catch {
        if (!cancelled) setCover({ identity, photo: null, label: "封面暂不可用" });
      }
    };
    if (typeof IntersectionObserver === "undefined") {
      load();
      return () => { cancelled = true; };
    }
    const observer = new IntersectionObserver(entries => {
      if (entries.some(entry => entry.isIntersecting)) { observer.disconnect(); load(); }
    }, { root: node.closest?.(".chat-sidebar") || null, rootMargin: "120px 0px" });
    observer.observe(node);
    return () => { cancelled = true; observer.disconnect(); };
  }, [identity]);
  return <span ref={coverRef} className="conversation-cover">
    {visibleCover.photo ? <img src={visibleCover.photo} alt="" loading="lazy" onError={() => setCover(previous => previous.identity === identity ? {identity,photo:null,label:"封面暂不可用"} : previous)} />
      : <span className="conversation-cover-placeholder"><UiIcon name="camera" size={20} /><span>{visibleCover.label}</span></span>}
  </span>;
}

function ChatPage(props) {
  return <ChatWorkspace key={props.currentUsername || "guest"} {...props} />;
}

function WorkspacePreparation({ brief, run, planId, draft, loading, associationError, onDraft, onOpen, onReply, onCancel, onRetry }) {
  const active = ["queued", "running", "waiting_user"].includes(run?.status);
  const data = active ? { ...(brief?.data || {}), ...(run?.request_snapshot || {}) }
    : { ...(run?.request_snapshot || {}), ...(brief?.data || {}) };
  data.trip_constraints = data.trip_constraints || data.effective_constraints || [];
  const view = ChatState.briefViewModel({ ...brief, data });
  const companion = (data.trip_constraints || []).find(item => item.category === "companion_context")?.value_text;
  const terminal = ["failed", "cancelled"].includes(run?.status);
  const title = loading ? "正在恢复这段对话" : terminal ? (run.status === "failed" ? "这次规划暂未完成" : "这次规划已取消")
    : active ? (run.status === "waiting_user" ? "需要你的回复" : "正在规划") : planId ? "行程预览" : brief?.status === "ready" ? "需求待确认" : "旅行准备";
  const destination = data.destination || "";
  const inspiration = destination ? SCENIC_SLIDES.find(item => item.destination === destination) : SCENIC_SLIDES[0];
  return <section className="workspace-preparation" aria-label="旅行准备与预览">
    <h3>{title}</h3>
    {!loading && <>
      {!active && !terminal && !planId && <figure className="workspace-inspiration">
        <WorkspacePhoto photo={inspiration?.src} alt={inspiration?.alt || `${destination}灵感图暂缺`} />
        <figcaption>{inspiration ? `灵感参考 · ${inspiration.title}` : "暂无此目的地的灵感图"}</figcaption>
        {inspiration && <small><a href={inspiration.source}>{inspiration.author}</a> · <a href={inspiration.licenseUrl}>{inspiration.license}</a></small>}
      </figure>}
      {(!planId || brief || run) && <dl className="workspace-known">
        <div><dt>目的地</dt><dd>{destination || "想去哪？"}</dd></div>
        <div><dt>日期 / 天数</dt><dd>{view.dateLabel}</dd></div>
        <div><dt>同行</dt><dd>{companion || "和谁一起？"}</dd></div>
      </dl>}
      {!!view.preferences.length && <ul className="workspace-preferences">{view.preferences.map(([label,value],index) => <li key={index}><small>{label}</small><span>{value}</span></li>)}</ul>}
      {!!view.missing.length && <p className="workspace-missing">待补充：{view.missing.join("、")}</p>}
      {!active && !terminal && !planId && <div className="workspace-examples"><p>在中间对话补充想去哪、几天、和谁一起。</p>
        {draft && <p className="workspace-draft"><small>尚未发送的想法</small>{draft}</p>}
        <button onClick={() => onDraft("想找一个适合两个人的周末目的地，慢慢逛。")}>试试这样问</button>
      </div>}
      {active && <div className={`workspace-run-state ${run.status}`} role="status">
        <strong>{run.status === "waiting_user" ? run.pending_interaction?.question || run.pending_interaction?.prompt || "请补充当前任务所需的信息。"
          : run.latest_progress_label || run.stage_label || run.agent_activity?.label || (run.status === "queued" ? "任务已排队" : "任务正在执行")}</strong>
        {run.status === "waiting_user" ? <button onClick={onReply}>回到对话回答</button> : <button onClick={onCancel}>停止当前规划</button>}
      </div>}
      {terminal && <div className="workspace-run-state" role={run.status === "failed" ? "alert" : "status"}>
        {run.status === "failed" && <p>{run.error_public?.message || "规划未能完成，请稍后再试。"}</p>}
        {run.error_public?.retryable !== false && <button onClick={onRetry}>重试这次规划</button>}
      </div>}
      {associationError && <p role="status">关联行程暂不可用，稍后会再次查询。</p>}
      {planId && <WorkspacePlanPreview key={planId} planId={planId} onOpen={onOpen} />}
    </>}
  </section>;
}

function WorkspacePhoto({ photo, alt }) {
  const [failed, setFailed] = React.useState(null);
  return <div className="workspace-photo">{photo && failed !== photo
    ? <img src={photo} alt={alt} loading="lazy" onError={() => setFailed(photo)} />
    : <span><UiIcon name="camera" size={22} />{alt} · 暂无可用图片</span>}</div>;
}

function WorkspacePlanPreview({ planId, onOpen }) {
  const [snapshot, setSnapshot] = React.useState(null);
  const [mapOpen, setMapOpen] = React.useState(false);
  const [dayIndex, setDayIndex] = React.useState(0);
  React.useEffect(() => {
    let cancelled = false;
    getHistoryItem(planId).then(item => {
      if (!cancelled) setSnapshot({ plan: item?.plan || null, error: !item?.plan });
    }).catch(() => { if (!cancelled) setSnapshot({ error: true }); });
    return () => { cancelled = true; };
  }, [planId]);
  const plan = snapshot?.plan;
  const days = Array.isArray(plan?.days) ? plan.days : [];
  const selectedDay = days[dayIndex];
  const points = (selectedDay?.timeline || []).filter(item => item.type === "attraction"
    && typeof item.location?.lat === "number" && typeof item.location?.lng === "number"
    && Number.isFinite(item.location.lat) && Number.isFinite(item.location.lng)
    && Math.abs(item.location.lat) <= 90 && Math.abs(item.location.lng) <= 180)
    .map(item => ({ lat: item.location.lat, lng: item.location.lng, name: item.name, label: item.name, kind: "attraction" }));
  const photos = days.flatMap(day => (day.timeline || []).filter(item => item.type === "attraction").map(item => ({ ...item, day: day.day }))).slice(0, 4);
  return <section className="workspace-plan-preview" aria-label="关联行程预览">
    <h4>已保存行程</h4>
    {!snapshot ? <p role="status">正在读取行程预览…</p> : snapshot.error ? <p role="status">行程预览暂不可用，可以打开完整行程。</p> : <>
      <WorkspacePhoto photo={firstDayFirstPoiPhoto({ plan })} alt={`${plan.destination || "关联行程"}封面`} />
      <strong>{plan.destination || "关联行程"} · {plan.days_count || days.length} 天</strong>
      {plan.start_date && plan.end_date && <p>{plan.start_date} — {plan.end_date}</p>}
      {plan.notes && <p>{plan.notes}</p>}
      <ol className="workspace-day-list">{days.map((day,index) => <li key={index}><b>Day {day.day || index + 1}</b><span>{day.theme || (day.timeline || []).filter(item => item.type === "attraction").map(item => item.name).join(" → ") || "当天安排待补充"}</span></li>)}</ol>
      {!!photos.length && <div className="workspace-poi-photos">{photos.map((item,index) => <figure key={index}><WorkspacePhoto photo={item.photo} alt={item.name || "景点"} /><figcaption>{item.name}</figcaption></figure>)}</div>}
      {days.length > 1 && <label className="workspace-map-day">地图日期<select aria-label="选择地图日期" value={dayIndex} onChange={event => { setDayIndex(Number(event.target.value)); setMapOpen(false); }}>{days.map((day,index) => <option key={index} value={index}>Day {day.day || index + 1}</option>)}</select></label>}
      {points.length ? <button aria-expanded={mapOpen} onClick={() => setMapOpen(!mapOpen)}>{mapOpen ? "收起路线地图" : "展开路线地图"}</button> : <p className="workspace-map-unavailable">当天暂无可用坐标</p>}
      {mapOpen && !!points.length && <WorkspaceActualMap key={dayIndex} points={points} />}
    </>}
    <button className="workspace-open-plan" onClick={() => onOpen?.(planId)}>查看完整行程<UiIcon name="arrow-right" size={16} /></button>
  </section>;
}

function WorkspaceActualMap({ points }) {
  const host = React.useRef(null);
  const [status, setStatus] = React.useState("loading");
  React.useEffect(() => {
    let cancelled = false;
    const node = host.current;
    Promise.resolve().then(() => initAmapForDay(node, points)).then(loaded => {
      if (cancelled) { destroyAmap(node); return; }
      setStatus(loaded ? "ready" : "error");
    }).catch(() => { if (!cancelled) setStatus("error"); });
    return () => { cancelled = true; destroyAmap(node); };
  }, []);
  return <div className="workspace-real-map"><div ref={host} className="workspace-map-host" />{status !== "ready" && <p role="status">{status === "loading" ? "正在加载真实路线地图…" : "地图暂不可用，请查看行程中的景点安排。"}</p>}</div>;
}

function ChatWorkspace({
  currentUsername, onRequestLogin, onOpenPlan,
  revisionTrigger, onRevisionConsumed,
  initialDraft = "", onInitialDraftConsumed,
}) {
  const [conversations, setConversations] = React.useState([]);
  const [activeId, setActiveId] = React.useState(null);
  const [state, setState] = React.useState(() => ChatState.initialState());
  const [draft, setDraft] = React.useState("");
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState("");
  const [sidebarOpen, setSidebarOpen] = React.useState(false);
  const [auxiliaryOpen, setAuxiliaryOpen] = React.useState(false);
  const historyToggleRef = React.useRef(null);
  const auxiliaryToggleRef = React.useRef(null);
  const historyCloseRef = React.useRef(null);
  const auxiliaryCloseRef = React.useRef(null);
  const previousPanelRef = React.useRef(null);
  const [visibleRunIds, setVisibleRunIds] = React.useState(() => new Set());
  const [observerReady, setObserverReady] = React.useState(false);
  const [composerTarget, setComposerTarget] = React.useState(null);
  const [revisionSubmitting, setRevisionSubmitting] = React.useState(false);
  const [compressing, setCompressing] = React.useState(false);
  const [compressionFeedback, setCompressionFeedback] = React.useState("");
  const [associationSnapshot, setAssociationSnapshot] = React.useState(null);
  const associationRevisionRef = React.useRef(null);
  const conversationRefreshRef = React.useRef(0);
  const stateVersionIdRef = React.useRef(null);
  const abortsRef = React.useRef({});
  const activeIdRef = React.useRef(null);
  const conversationEpochRef = React.useRef(0);
  const mountedRef = React.useRef(true);
  const captureConversation = () => {
    const epoch = conversationEpochRef.current;
    return () => mountedRef.current && epoch === conversationEpochRef.current;
  };
  const runNodesRef = React.useRef({});
  const composerRef = React.useRef(null);
  const firstJourneyRef = React.useRef(null);
  const errorRef = React.useRef(null);
  const waitingRunsRef = React.useRef(new Set());
  const consumedRevisionNoncesRef = React.useRef(new Set());
  const activityItems = ChatState.activityItems(state);
  const runList = Object.values(state.runs).sort(
    (a, b) => String(a.created_at || "").localeCompare(String(b.created_at || ""))
  );
  const blockingRuns = runList.filter(run =>
    run.kind === "chat" && ["queued", "running", "waiting_user"].includes(run.status)
  );
  const blockingRun = blockingRuns.find(run => run.status === "waiting_user")
    || blockingRuns[blockingRuns.length - 1]
    || null;
  const activeRuns = blockingRun?.journey_step_index !== undefined ? [blockingRun] : [];
  const activeConversation = conversations.find(item => item.id === activeId) || null;
  const conversationArchived = activeConversation?.status === "archived";
  const hasConversationMessages = Object.keys(state.messages || {}).length > 0;
  const stateVersionId = latestConversationItinerary(Object.values(state.messages), runList);
  stateVersionIdRef.current = stateVersionId;
  const associationMatches = associationSnapshot?.conversationId === activeId
    && associationSnapshot?.stateVersionId === stateVersionId;
  const currentItineraryId = associationMatches ? associationSnapshot.itineraryId : stateVersionId;
  const associationError = associationMatches && associationSnapshot.error;
  const preparationBrief = state.briefs[state.currentBriefId] || Object.values(state.briefs || {}).filter(item => item.status !== "discarded")
    .sort((a,b) => String(b.updated_at || b.created_at || "").localeCompare(String(a.updated_at || a.created_at || "")))[0];
  const preparationRuns = runList.filter(item => ["chat", "travel_plan", "revision"].includes(item.kind));
  const latestPreparationRun = preparationRuns.find(item => item.status === "waiting_user")
    || preparationRuns.filter(item => ["queued", "running"].includes(item.status)).slice(-1)[0]
    || preparationRuns.slice(-1)[0];
  const briefTime = preparationBrief?.updated_at || preparationBrief?.created_at;
  const runTime = latestPreparationRun?.updated_at || latestPreparationRun?.created_at;
  const newPreparation = ["collecting", "ready"].includes(preparationBrief?.status)
    && ["succeeded", "failed", "cancelled"].includes(latestPreparationRun?.status)
    && (!briefTime || !runTime || Date.parse(briefTime) >= Date.parse(runTime));
  const preparationRun = newPreparation ? null : latestPreparationRun;
  const closePanels = () => { setSidebarOpen(false); setAuxiliaryOpen(false); };
  React.useEffect(() => {
    if (!window.matchMedia("(max-width: 900px)").matches) return;
    const panel = sidebarOpen ? "history" : auxiliaryOpen ? "auxiliary" : null;
    if (panel) (panel === "history" ? historyCloseRef : auxiliaryCloseRef).current?.focus();
    else if (previousPanelRef.current) {
      (previousPanelRef.current === "history" ? historyToggleRef : auxiliaryToggleRef).current?.focus();
    }
    previousPanelRef.current = panel;
  }, [sidebarOpen, auxiliaryOpen]);

  React.useEffect(() => {
    if (!initialDraft) return;
    setDraft(initialDraft);
    window.setTimeout(() => composerRef.current?.focus(), 0);
    onInitialDraftConsumed?.();
  }, [initialDraft, onInitialDraftConsumed]);

  const sidebarConversations = conversations.map(item => {
    if (item.id !== activeId || item.status === "archived") return item;
    const chatRuns = runList.filter(run => run.kind === "chat");
    return {
      ...item,
      has_waiting_user: item.has_waiting_user
        || chatRuns.some(run => run.status === "waiting_user"),
      has_ready_brief: item.has_ready_brief
        || Object.values(state.briefs).some(brief => brief.status === "ready"),
      has_active_planning: item.has_active_planning
        || chatRuns.some(run => ["queued", "running"].includes(run.status)
          && run.journey_step_index !== undefined),
    };
  });

  const persistCursor = (runId, sequence) => {
    if (!sequence) return;
    localStorage.setItem(`run-cursor:${runId}`, String(sequence));
  };

  const applyRunEvent = React.useCallback((runId, event) => {
    setState(previous => {
      const next = ChatState.applyEvent(previous, runId, event);
      persistCursor(runId, next.cursors[runId]);
      return next;
    });
  }, []);

  const markViewedIfVisible = React.useCallback(async conversationId => {
    const isCurrent = captureConversation();
    if (!ChatState.shouldMarkConversationViewed(
      document.visibilityState, activeIdRef.current, conversationId
    )) return;
    try {
      const updated = await markConversationViewed(conversationId);
      if (!isCurrent()) return;
      setConversations(previous => previous.map(item => (
        item.id === updated.id ? { ...item, ...updated } : item
      )));
    } catch {}
  }, []);

  const subscribeRun = React.useCallback((run, explicitCursor = null) => {
    if (!run?.id || abortsRef.current[run.id]) return;
    const isCurrent = captureConversation();
    const cursor = explicitCursor === null
      ? Number(localStorage.getItem(`run-cursor:${run.id}`) || 0)
      : Number(explicitCursor);
    streamRuntimeRun(run.id, cursor, {
      onAbort: abort => { if (isCurrent()) abortsRef.current[run.id] = abort; else abort(); },
      onEvent: event => {
        if (!isCurrent()) return;
        applyRunEvent(run.id, event);
        const completed = (
          event.payload?.kind === "run.status" && event.payload.status === "succeeded"
        ) || (event.kind === "end" && event.payload?.status === "succeeded");
        if (run.kind === "chat" && completed && run.journey_step_index !== undefined) {
          markViewedIfVisible(run.conversation_id);
        }
      },
      onClose: () => { if (isCurrent()) delete abortsRef.current[run.id]; },
      onError: () => { if (isCurrent()) delete abortsRef.current[run.id]; },
    });
  }, [applyRunEvent, markViewedIfVisible]);

  const loadConversation = React.useCallback(async (conversationId) => {
    conversationEpochRef.current += 1;
    const isCurrent = captureConversation();
    if (activeIdRef.current && activeIdRef.current !== conversationId) setDraft("");
    activeIdRef.current = conversationId;
    setActiveId(conversationId);
    setLoading(true);
    setError("");
    setCompressionFeedback("");
    setCompressing(false);
    setRevisionSubmitting(false);
    setState(ChatState.initialState());
    setAssociationSnapshot(null);
    associationRevisionRef.current = null;
    setComposerTarget(null);
    Object.values(abortsRef.current).forEach(abort => abort());
    abortsRef.current = {};
    try {
      const [messages, runs, brief] = await Promise.all([
        getConversationMessages(conversationId),
        listRuns(conversationId),
        getActivePlanningBrief(conversationId),
      ]);
      const activeRuns = runs.filter(
        run => run.kind === "chat" && ["queued", "running", "waiting_user"].includes(run.status)
      );
      const restorableRuns = runs.filter(run => run.kind === "chat"
        && ["queued", "running", "waiting_user", "failed", "cancelled"].includes(run.status));
      const eventHistories = await Promise.all(
        restorableRuns.map(async run => {
          try { return [run.id, await getRunEvents(run.id, 0)]; }
          catch (error) {
            if (["failed", "cancelled"].includes(run.status)) return [run.id, []];
            throw error;
          }
        })
      );
      if (!isCurrent()) return;
      let next = ChatState.initialState();
      messages.forEach(message => { next = ChatState.upsertMessage(next, message); });
      runs.forEach(run => {
        const previous = next.runs[run.id] || {};
        next.runs[run.id] = { ...previous, ...run };
      });
      if (brief) { next.currentBriefId = brief.id; next.briefs[brief.id] = {
        id: brief.id,
        status: brief.status,
        data: brief.data || {},
        missing_fields: brief.missing_fields || [],
        created_at: brief.created_at,
        updated_at: brief.updated_at,
      }; }
      eventHistories.forEach(([runId, events]) => {
        events.forEach(event => {
          next = ChatState.applyEvent(next, runId, event);
        });
        persistCursor(runId, next.cursors[runId]);
      });
      // A bounded historical page may stop before the terminal event. Keep
      // the authoritative terminal snapshot while retaining derived progress.
      runs.filter(run => ["failed", "cancelled"].includes(run.status)).forEach(run => {
        next.runs[run.id] = { ...next.runs[run.id], ...run };
      });
      setState(next);
      setComposerTarget(null);
      setSidebarOpen(false);
      activeRuns.forEach(
        run => subscribeRun(run, next.cursors[run.id] || 0)
      );
      await markViewedIfVisible(conversationId);
    } catch (e) {
      if (isCurrent()) setError(e.message || "加载对话失败");
    } finally {
      if (isCurrent()) setLoading(false);
    }
  }, [subscribeRun, markViewedIfVisible]);

  React.useEffect(() => {
    if (!currentUsername) { onRequestLogin?.(); return; }
    let alive = true;
    const isCurrent = captureConversation();
    listConversations().then(async items => {
      if (!alive || !isCurrent()) return;
      setConversations(items);
      const initial = items.find(item => item.status !== "archived") || items[0];
      if (initial) await loadConversation(initial.id);
      else setLoading(false);
    }).catch(e => { if (alive && isCurrent()) { setError(e.message); setLoading(false); } });
    return () => {
      alive = false;
      mountedRef.current = false;
      conversationEpochRef.current += 1;
      Object.values(abortsRef.current).forEach(abort => abort());
    };
  }, [currentUsername]); // eslint-disable-line

  // 没有行程时，以「生成中的旅程」取代空白聊天舞台；系统要求减少动态时完全跳过。
  React.useEffect(() => {
    if (loading || activityItems.length || !firstJourneyRef.current || !window.gsap) return undefined;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return undefined;
    const root = firstJourneyRef.current;
    const ctx = window.gsap.context(() => {
      const tl = window.gsap.timeline({ defaults: { ease: "power3.out" } });
      tl.from(".first-journey-orbit", { scale: .72, autoAlpha: 0, duration: .72 })
        .from(".first-journey-route path", { strokeDashoffset: 220, duration: .9, stagger: .1 }, "<.12")
        .from(".first-journey-pin", { y: 18, scale: .25, autoAlpha: 0, duration: .42, stagger: .11, ease: "back.out(1.8)" }, "<.28")
        .from(".first-journey-copy > *", { y: 15, autoAlpha: 0, duration: .45, stagger: .1 }, "<.05");
      window.gsap.to(".first-journey-sun", { y: -7, duration: 2.1, ease: "sine.inOut", repeat: -1, yoyo: true });
    }, root);
    return () => ctx.revert();
  }, [loading, activityItems.length]);

  React.useEffect(() => {
    if (!currentUsername) return;
    let active = true;
    const refreshConversations = async () => {
      if (!ChatState.shouldPollConversations(document.visibilityState)) return;
      const refreshRequest = ++conversationRefreshRef.current;
      try {
        const items = await listConversations();
        if (!active || refreshRequest !== conversationRefreshRef.current) return;
        setConversations(items);
        const selected = items.find(item => item.id === activeId);
        if (!selected || selected.updated_at === associationRevisionRef.current) return;
        const isCurrent = captureConversation();
        const baseVersion = stateVersionIdRef.current;
        try {
          const [messages, runs] = await Promise.all([
            getConversationMessages(activeId), listRuns(activeId),
          ]);
          if (!active || !isCurrent() || refreshRequest !== conversationRefreshRef.current
            || baseVersion !== stateVersionIdRef.current) return;
          setAssociationSnapshot({ conversationId:activeId, stateVersionId:baseVersion,
            itineraryId:latestConversationItinerary(messages, runs), error:false });
          associationRevisionRef.current = selected.updated_at;
        } catch {
          if (active && isCurrent() && refreshRequest === conversationRefreshRef.current
            && baseVersion === stateVersionIdRef.current) {
            setAssociationSnapshot({ conversationId:activeId, stateVersionId:baseVersion, itineraryId:null, error:true });
          }
        }
      } catch {}
    };
    const handleVisibility = async () => {
      if (!ChatState.shouldPollConversations(document.visibilityState)) return;
      await markViewedIfVisible(activeId);
      await refreshConversations();
    };
    document.addEventListener("visibilitychange", handleVisibility);
    const timer = window.setInterval(refreshConversations, 4000);
    return () => {
      active = false;
      document.removeEventListener("visibilitychange", handleVisibility);
      window.clearInterval(timer);
    };
  }, [currentUsername, activeId, markViewedIfVisible]);

  const newConversation = async () => {
    const isCurrent = captureConversation();
    try {
      const created = await createConversation("新的旅行对话");
      if (!isCurrent()) return null;
      setConversations(previous => [created, ...previous]);
      await loadConversation(created.id);
      return created;
    } catch (e) {
      if (isCurrent()) setError(e.message || "暂时无法新建对话");
      return null;
    }
  };

  React.useEffect(() => {
    if (typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver(entries => {
      setVisibleRunIds(previous => {
        const next = new Set(previous);
        entries.forEach(entry => {
          const runId = entry.target.dataset.runId;
          if (entry.isIntersecting) next.add(runId);
          else next.delete(runId);
        });
        return next;
      });
      setObserverReady(true);
    }, { threshold: 0.3 });
    Object.values(runNodesRef.current).filter(Boolean).forEach(node => observer.observe(node));
    return () => observer.disconnect();
  }, [activityItems.map(item => item.key).join("|")]);

  const submitChatContent = async (content, target, requestedConversationId = activeId) => {
    const isCurrent = captureConversation();
    let conversationId = requestedConversationId;
    if (!conversationId) {
      const created = await createConversation(content.slice(0, 20));
      if (!isCurrent()) return null;
      setConversations(previous => [created, ...previous]);
      conversationId = created.id;
      activeIdRef.current = conversationId;
      setActiveId(conversationId);
    }
    const context = target?.mode === "revision"
      ? { related_itinerary_id: target.itineraryId }
      : {};
    const result = await submitConversationMessage(conversationId, content, context);
    if (!isCurrent()) return null;
    setConversations(previous => previous.map(item => (
      item.id === conversationId && (!item.title || item.title === "新的旅行对话")
        ? { ...item, title: content.slice(0, 24), updated_at: new Date().toISOString() }
        : item
    )));
    setState(previous => {
      let next = ChatState.upsertMessage(previous, result.message);
      next = { ...next, runs: { ...next.runs, [result.run.id]: result.run } };
      return next;
    });
    subscribeRun(result.run);
    return result;
  };

  const send = async () => {
    let isCurrent = captureConversation();
    if (revisionSubmitting) return;
    const content = draft.trim();
    if (!content) return;
    const targetRun = composerTarget?.mode === "resume" ? state.runs[composerTarget.run.id] : null;
    if (composerTarget?.mode === "resume" && targetRun?.status !== "waiting_user") {
      setComposerTarget(null);
      setError("这个问题已结束，请在对话中继续描述旅行想法。");
      return;
    }
    if (targetRun && targetRun.pending_interaction?.interaction_id !== composerTarget.run.pending_interaction?.interaction_id) {
      setComposerTarget({ ...composerTarget, run: targetRun });
      setError("任务的问题已经更新，请先查看新问题再回答。");
      return;
    }
    const answerRun = blockingRun?.status === "waiting_user" ? blockingRun
      : targetRun;
    if (answerRun) {
      const run = answerRun;
      const interaction = run.pending_interaction;
      if (!interaction?.interaction_id) return;
      setDraft("");
      setError("");
      try {
        const resumed = await resumeRuntimeRun(run.id, interaction.interaction_id, content);
        if (!isCurrent()) return;
        setState(previous => {
          let next = previous;
          if (resumed.accepted_message) next = ChatState.upsertMessage(next, resumed.accepted_message);
          return {
            ...next,
            runs: { ...next.runs, [run.id]: { ...next.runs[run.id], ...resumed, pending_interaction: null } },
          };
        });
        setComposerTarget(null);
      } catch (e) {
        if (!isCurrent()) return;
        setDraft(content);
        setError(e.message || "回复提交失败，请重试");
      }
      return;
    }
    if (blockingRun) return;
    setDraft("");
    setError("");
    try {
      let conversationId = activeId;
      if (composerTarget?.mode === "revision" && (!conversationId || conversationArchived)) {
        const created = await newConversation();
        if (!created) throw new Error("暂时无法新建对话");
        conversationId = created.id;
        if (activeIdRef.current !== conversationId || !mountedRef.current) return;
        isCurrent = captureConversation();
      }
      const result = await submitChatContent(content, composerTarget, conversationId);
      if (!result || !isCurrent()) return;
      setComposerTarget(null);
    } catch (e) {
      if (!isCurrent()) return;
      setDraft(content);
      setError(e.message || "发送失败");
      if (e.code === "conversation_run_active" && activeId) await loadConversation(activeId);
    }
  };

  React.useEffect(() => {
    if (!revisionTrigger || loading || !currentUsername) return;
    const nonce = revisionTrigger.nonce;
    if (consumedRevisionNoncesRef.current.has(nonce)) return;
    consumedRevisionNoncesRef.current.add(nonce);
    const target = {
      mode: "revision",
      itineraryId: revisionTrigger.itineraryId,
      label: "从行程详情发起的修改",
    };
    const content = String(revisionTrigger.content || "").trim();
    setComposerTarget(target);
    setDraft("");
    setError("");
    setRevisionSubmitting(true);
    (async () => {
      let isCurrent = captureConversation();
      try {
        let conversationId = activeId;
        if (!conversationId || conversationArchived) {
          const created = await newConversation();
          if (!created) throw new Error("暂时无法新建对话");
          conversationId = created.id;
          if (activeIdRef.current !== conversationId || !mountedRef.current) return;
          isCurrent = captureConversation();
          setComposerTarget(target);
        }
        const result = await submitChatContent(content, target, conversationId);
        if (!result || !isCurrent()) return;
        setComposerTarget(null);
      } catch (e) {
        if (!isCurrent()) return;
        setDraft(content);
        setComposerTarget(target);
        setError(e.message || "修改请求发送失败，请重试");
      } finally {
        if (isCurrent()) setRevisionSubmitting(false);
        if (mountedRef.current) onRevisionConsumed?.(nonce);
      }
    })();
  }, [revisionTrigger?.nonce, loading, currentUsername]); // eslint-disable-line

  const isVisibleConversation = captureConversation();
  const refreshBrief = brief => {
    if (!isVisibleConversation()) return;
    const visibleBrief = {
      id: brief.id,
      status: brief.status,
      data: brief.data || {},
      missing_fields: brief.missing_fields || [],
      created_at: brief.created_at,
      updated_at: brief.updated_at,
    };
    setState(previous => ({
      ...previous,
      briefs: { ...previous.briefs, [brief.id]: visibleBrief },
      currentBriefId: brief.status === "discarded" ? null : brief.id,
    }));
  };

  const submitBrief = async brief => {
    if (blockingRun) throw new Error("当前对话已有任务进行中");
    await submitChatContent("开始规划这份行程", null, activeId);
  };

  const discardBrief = async brief => {
    const result = await discardPlanningBrief(brief.id);
    refreshBrief(result);
  };

  const archiveCurrent = async () => {
    if (!activeConversation || conversationArchived) return;
    setError("");
    const isCurrent = captureConversation();
    try {
      const archived = await archiveConversation(activeConversation.id);
      if (!isCurrent()) return;
      setConversations(previous => previous.map(item => item.id === archived.id ? { ...item, ...archived } : item));
      setComposerTarget(null);
      setDraft("");
    } catch (e) {
      if (isCurrent()) setError(e.message || "归档失败，请重试");
    }
  };

  const compressCurrent = async () => {
    if (!activeConversation || conversationArchived || compressing) return;
    setCompressing(true);
    setCompressionFeedback("");
    setError("");
    const isCurrent = captureConversation();
    try {
      const result = await compressConversation(activeConversation.id);
      if (!isCurrent()) return;
      setCompressionFeedback(result.compressed
        ? `已将较早对话整理到第 ${result.summarized_through_sequence} 条，最近 ${result.recent_turns_kept} 轮保持原文。`
        : `当前完整对话不足 ${result.recent_turns_kept + 1} 轮，暂时无需压缩。`);
    } catch (e) {
      if (isCurrent()) setError(e.message || "主动压缩失败，原始消息没有变化");
    } finally {
      if (isCurrent()) setCompressing(false);
    }
  };

  const retryMemory = async () => {
    if (!activeConversation) return;
    const isCurrent = captureConversation();
    try {
      await retryConversationMemory(activeConversation.id);
      if (!isCurrent()) return;
      setConversations(previous => previous.map(item => item.id === activeConversation.id
        ? { ...item, finalization_status: "pending", memory_error_code: null }
        : item));
    } catch (e) {
      if (isCurrent()) setError(e.message || "记忆整理重试失败");
    }
  };

  const controlRun = async (run, action) => {
    const isCurrent = captureConversation();
    try {
      const result = action === "cancel"
        ? await cancelRuntimeRun(run.id)
        : await retryRuntimeRun(run.id);
      if (!isCurrent()) return;
      setState(previous => ({
        ...previous,
        runs: { ...previous.runs, [result.id]: { ...previous.runs[result.id], ...result } },
      }));
      if (action === "retry") subscribeRun(result);
    } catch (e) {
      if (isCurrent()) setError(e.message || "操作失败");
    }
  };

  const focusRun = runId => {
    const node = runNodesRef.current[runId];
    if (!node) return;
    node.scrollIntoView({ behavior: "smooth", block: "center" });
    window.setTimeout(() => node.focus(), 350);
  };

  React.useEffect(() => {
    if (!loading && activeId && !composerTarget) composerRef.current?.focus();
  }, [loading, activeId]); // eslint-disable-line

  React.useEffect(() => {
    if (error) errorRef.current?.focus();
  }, [error]);

  React.useEffect(() => {
    const waiting = new Set(
      runList.filter(run => run.status === "waiting_user").map(run => run.id)
    );
    const newlyWaiting = [...waiting].find(id => !waitingRunsRef.current.has(id));
    waitingRunsRef.current = waiting;
    if (newlyWaiting) window.setTimeout(() => focusRun(newlyWaiting), 0);
  }, [runList.map(run => `${run.id}:${run.status}`).join("|")]); // eslint-disable-line

  const registerRunNode = (runId, node) => {
    if (node) runNodesRef.current[runId] = node;
    else delete runNodesRef.current[runId];
  };

  const offscreenRuns = observerReady
    ? activeRuns.filter(run => !visibleRunIds.has(run.id))
    : [];

  return (
    <div className={`chat-shell workspace-shell page-fade ${sidebarOpen || auxiliaryOpen ? "mobile-panel-open" : ""}`}
      onKeyDown={event => { if (event.key === "Escape") closePanels(); }}>
      <aside id="workspace-history" className={`chat-sidebar ${sidebarOpen ? "open" : ""}`} aria-label="旅行工作区记录">
        <button ref={historyCloseRef} className="workspace-panel-close" onClick={closePanels} aria-label="关闭旅行对话列表"><UiIcon name="close" size={18} />返回对话</button>
        <div className="chat-sidebar-head">
          <div><small>MY JOURNEYS</small><strong>旅行线索</strong></div>
          <button onClick={newConversation} aria-label="开始一段新的旅行规划"><UiIcon name="plus" size={15} />新旅程</button>
        </div>
        <div className="conversation-list">
          {sidebarConversations.map(item => {
            const attention = ChatState.conversationAttention(item);
            const active = activeId === item.id;
            const ariaStatus = attention && attention.kind !== "archived"
              ? `，${attention.ariaLabel}` : attention?.ariaLabel ? `，${attention.ariaLabel}` : "";
            return (
              <button key={item.id}
                className={`conversation-item ${active ? "active" : ""} attention-${attention?.kind || "none"}`}
                aria-label={`${item.title || "未命名对话"}${ariaStatus}`}
                aria-current={active ? "true" : undefined}
                onClick={() => loadConversation(item.id)}>
                <ConversationCover conversation={item} currentUsername={currentUsername}
                  planId={active ? currentItineraryId : undefined} associationError={active && associationError} />
                <span className="conversation-item-main">
                  <strong>{item.title || "未命名对话"}</strong>
                  {attention && attention.kind !== "archived" && (
                    <span className={`conversation-attention ${attention.kind}`} aria-label={attention.ariaLabel}>
                      {attention.kind === "planning" && <i className="conversation-spinner" aria-hidden="true" />}
                      {attention.kind === "unread" && <i className="conversation-unread-dot" aria-hidden="true" />}
                      {attention.label}
                    </span>
                  )}
                </span>
                <small>{new Date(item.updated_at).toLocaleDateString()}{attention?.kind === "archived" ? " · 已归档" : ""}</small>
              </button>
            );
          })}
        </div>
      </aside>
      <main className="chat-main">
        <div className="chat-header">
          <button className="chat-sidebar-toggle" onClick={() => setSidebarOpen(value => !value)}
            ref={historyToggleRef} aria-controls="workspace-history"
            aria-expanded={sidebarOpen} aria-label="打开旅行对话列表"><UiIcon name="menu" size={19} /></button>
          <div className="chat-title-copy">
            <h2>旅行工作区</h2>
            <p>{conversationArchived ? "这段旅程已归档，小way正在把有用的旅行习惯整理进画像。" : "行程、路线和细节在这里生长；需要时，再叫小way一起推敲。"}</p>
          </div>
          <button ref={auxiliaryToggleRef} className="workspace-aux-toggle" aria-label="打开旅行辅助" aria-controls="workspace-auxiliary"
            aria-expanded={auxiliaryOpen} onClick={() => { setSidebarOpen(false); setAuxiliaryOpen(value => !value); }}><UiIcon name="menu" size={17} />旅行辅助</button>
          {activeConversation && !conversationArchived && (
            <div className="chat-memory-actions">
              {hasConversationMessages && <button className="chat-compress-btn" disabled={compressing} onClick={compressCurrent} aria-label="立即压缩较早的完整对话轮次">
                <UiIcon name="compress" size={15} />{compressing ? "正在压缩…" : "主动压缩"}
              </button>}
              <button className="chat-archive-btn" onClick={archiveCurrent}><UiIcon name="archive" size={15} />归档对话</button>
            </div>
          )}
          {conversationArchived && (
            <span className={`memory-finalization ${activeConversation.finalization_status || "pending"}`}>
              {activeConversation.finalization_status === "succeeded" ? "记忆已整理" : activeConversation.finalization_status === "failed" ? "记忆整理失败" : "记忆整理中"}
            </span>
          )}
          {blockingRun && <span className="chat-task-count">当前对话处理中</span>}
        </div>
        {compressionFeedback && <div className="chat-compression-feedback" role="status">
          <UiIcon name="check" size={14} />{compressionFeedback}
        </div>}
        <div className="chat-feed" role="log" aria-label="旅行对话活动" aria-live="off">
          {loading && <div className="chat-empty">正在恢复对话…</div>}
          {!loading && activityItems.length === 0 && (
            <div ref={firstJourneyRef} className="first-journey">
              <div className="first-journey-orbit" aria-hidden="true">
                <i className="first-journey-sun" />
                <svg className="first-journey-route" viewBox="0 0 360 164" fill="none"><path d="M22 123C76 125 83 42 145 77s67 66 111 22c26-27 53-17 80-8" /></svg>
                <b className="first-journey-pin pin-a">1</b><b className="first-journey-pin pin-b">2</b><b className="first-journey-pin pin-c">3</b>
                <span>下一段旅程</span>
              </div>
              <div className="first-journey-copy">
                <span className="chat-empty-kicker">YOUR NEXT ITINERARY</span>
                <strong>先写下目的地，<br />我们把它铺成路线。</strong>
                <p>不必把对话当作目的地。说出一个念头，下一屏就会是一份可以编辑的旅行计划。</p>
                <div className="chat-empty-prompts">
                  <button onClick={() => setDraft("十月适合去云南吗？")}>找找灵感<small>十月适合去云南吗？</small></button>
                  <button onClick={() => setDraft("帮我规划去云南旅行")}>创建行程<small>帮我规划去云南旅行</small></button>
                </div>
              </div>
            </div>
          )}
          <ActivityTimeline
            items={activityItems}
            currentUsername={currentUsername}
            onBriefUpdate={refreshBrief}
            onBriefSubmit={submitBrief}
            onBriefDiscard={discardBrief}
            onRunCancel={run => controlRun(run, "cancel")}
            onRunRetry={run => controlRun(run, "retry")}
            onChatRetry={run => controlRun(run, "retry")}
            onRunOpen={run => run.result_itinerary_id && onOpenPlan?.(run.result_itinerary_id)}
            onRunModify={run => {
              setComposerTarget({
                mode: "revision",
                itineraryId: run.result_itinerary_id,
                label: `${run.request_snapshot?.destination || "这份行程"} · 继续修改`,
              });
              setDraft("");
            }}
            onRunReply={run => {
              setComposerTarget({ mode: "resume", run, label: `${run.request_snapshot?.destination || "规划任务"} · 回复` });
              setDraft("");
            }}
            registerRunNode={registerRunNode}
            onItineraryOpen={itineraryId => onOpenPlan?.(itineraryId)}
          />
        </div>
        <div className="chat-status-live" aria-live="polite" aria-atomic="true">
          {runList.find(run => run.status === "waiting_user")
            ? "有一项旅行规划需要你的回复"
            : runList.find(run => run.status === "failed")
              ? "有一项旅行规划未能完成"
              : ""}
        </div>
        {error && <div ref={errorRef} tabIndex="-1" className="chat-error" role="alert">{error}</div>}
        {offscreenRuns.length > 0 && (
          <div className="active-run-rail" aria-label="视口外的活动任务">
            {offscreenRuns.slice(0, 3).map(run => (
              <button key={run.id} onClick={() => focusRun(run.id)}>
                <span>{run.status === "waiting_user" ? "需要回复" : run.status === "queued" ? "等待开始" : "正在规划"}</span>
                <strong>{run.request_snapshot?.destination || (run.kind === "revision" ? "行程修改" : "旅行规划")}</strong>
                <span aria-hidden="true">↗</span>
              </button>
            ))}
          </div>
        )}
        <div className={`chat-composer-wrap ${conversationArchived ? "archived" : ""}`}>
          {conversationArchived ? (
            <div className="archived-conversation-note">
              <div><span>ARCHIVED</span><strong>这段旅途对话已经收好</strong><small>继续聊时会创建新对话，并使用已整理完成的最新记忆。</small></div>
              {activeConversation.finalization_status === "failed"
                ? <button onClick={retryMemory}>重新整理记忆</button>
                : <button onClick={newConversation}>开始新对话</button>}
            </div>
          ) : <>
          <div className="chat-composer">
            {composerTarget?.mode === "revision" && !blockingRun && (
              <div className="composer-target">
                <span>{composerTarget.mode === "resume" ? "正在回复任务" : "正在修改行程"}</span>
                <strong>{composerTarget.label}</strong>
                <button onClick={() => setComposerTarget(null)} aria-label="取消指定任务回复"><UiIcon name="close" size={15} /></button>
              </div>
            )}
            <textarea ref={composerRef} value={draft} onChange={e => setDraft(e.target.value)}
              disabled={!!blockingRun && blockingRun.status !== "waiting_user"}
              aria-label={blockingRun?.status === "waiting_user" ? "回答当前问题" : composerTarget ? composerTarget.label : "给小way发送消息"}
              placeholder={blockingRun?.status === "waiting_user" ? "回答上方问题，继续这次规划…" : blockingRun ? "规划进行中" : composerTarget?.mode === "revision" ? "说说你想怎么调整…" : "继续聊天，或描述一趟想规划的旅行…"}
              onKeyDown={e => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault(); send();
                }
              }} />
            <button className={`composer-send ${blockingRun && blockingRun.status !== "waiting_user" ? "is-stop" : ""}`}
              onClick={blockingRun && blockingRun.status !== "waiting_user" ? () => controlRun(blockingRun, "cancel") : send}
              disabled={blockingRun?.status === "waiting_user" ? !draft.trim() : !blockingRun && (!draft.trim() || revisionSubmitting)}
              aria-label={blockingRun && blockingRun.status !== "waiting_user" ? "停止当前任务" : "发送消息"}>
              <span aria-hidden="true"><UiIcon name={blockingRun && blockingRun.status !== "waiting_user" ? "stop" : "arrow-up"} size={18} /></span>
              <span className="composer-send-label">{blockingRun && blockingRun.status !== "waiting_user" ? "停止" : "发送"}</span>
            </button>
          </div>
          <small className="composer-hint">{blockingRun?.status === "waiting_user" ? "这条回答会继续当前任务，不会创建新的 Run" : blockingRun ? "当前对话会在任务完成后恢复输入" : revisionSubmitting ? "正在提交这次行程修改…" : composerTarget ? "这条内容会发送到指定任务" : "可以继续描述你的旅行想法"}</small>
          </>}
        </div>
      </main>
      <aside id="workspace-auxiliary" className={`workspace-auxiliary ${auxiliaryOpen ? "open" : ""}`} aria-label="旅行辅助">
        <button ref={auxiliaryCloseRef} className="workspace-panel-close" onClick={closePanels} aria-label="关闭旅行辅助"><UiIcon name="close" size={18} />返回对话</button>
        <small className="workspace-aux-kicker">YOUR JOURNEY</small>
        <h3>旅行辅助</h3>
        <p className="workspace-conversation-title">{activeConversation?.title || "新的旅行对话"}</p>
        <WorkspacePreparation key={activeId || "new"} brief={preparationBrief} run={preparationRun}
          planId={currentItineraryId} draft={draft} loading={loading} associationError={associationError} onOpen={onOpenPlan}
          onDraft={value => { setDraft(value); closePanels(); window.setTimeout(() => composerRef.current?.focus(), 0); }}
          onReply={() => { setComposerTarget({ mode: "resume", run: preparationRun, label: "规划任务 · 回复" }); closePanels(); window.setTimeout(() => composerRef.current?.focus(), 0); }}
          onCancel={() => { closePanels(); controlRun(preparationRun, "cancel"); }}
          onRetry={() => { closePanels(); controlRun(preparationRun, "retry"); }} />
      </aside>
    </div>
  );
}

function firstDayFirstPoiPhoto(data) {
  const timeline = data?.plan?.days?.[0]?.timeline;
  if (!Array.isArray(timeline)) return null;
  const poi = timeline.find(item => item?.type === "attraction");
  const photo = poi?.photo;
  return typeof photo === "string" && photo.trim() ? photo.trim() : null;
}

/* 历史列表只携带摘要；封面进入视口后从完整行程接口读取 Day 1 的第一个景点图。 */
function ItineraryCover({ planId, className, placeholder = "" }) {
  const coverRef = React.useRef(null);
  const [photo, setPhoto] = React.useState(null);

  React.useEffect(() => {
    const node = coverRef.current;
    if (!node) return undefined;
    let cancelled = false;
    const loadPhoto = () => {
      getHistoryItem(planId)
        .then(data => { if (!cancelled) setPhoto(firstDayFirstPoiPhoto(data)); })
        .catch(() => { if (!cancelled) setPhoto(null); });
    };
    if (typeof IntersectionObserver === "undefined") { loadPhoto(); return () => { cancelled = true; }; }
    const observer = new IntersectionObserver(entries => {
      if (entries.some(entry => entry.isIntersecting)) {
        observer.disconnect();
        loadPhoto();
      }
    }, { rootMargin: "160px 0px" });
    observer.observe(node);
    return () => { cancelled = true; observer.disconnect(); };
  }, [planId]);

  return <span ref={coverRef} className={`${className}${photo ? " has-photo" : ""}`}
    style={photo && !placeholder ? { backgroundImage: `url('${photo}')` } : undefined} aria-hidden="true">
    {placeholder && (photo
      ? <img src={photo} alt="" onError={() => setPhoto(null)} />
      : <span className="dashboard-cover-placeholder"><UiIcon name="camera" size={24} />{placeholder}</span>)}
  </span>;
}

function JourneyPlans({ sectionRef, sentinelRef, plans, loading, loadingMore, error, hasMore, onRetry, onOpen }) {
  const formatDates = trip => {
    const start = trip.start_date ? trip.start_date.replace(/-/g, ".").slice(5) : "";
    const end = trip.end_date ? trip.end_date.replace(/-/g, ".").slice(5) : "";
    return start && end ? `${start} — ${end}` : (trip.created_at || "").slice(0, 10).replace(/-/g, ".");
  };
  const dayCount = trip => trip.days_count || (trip.start_date && trip.end_date
    ? Math.ceil((new Date(trip.end_date) - new Date(trip.start_date)) / 86400000) + 1 : 1);

  return (
    <section ref={sectionRef} className="journey-plans" id="my-plans" aria-labelledby="my-plans-title">
      <div className="journey-plans-head">
        <div><span>MY PLANS</span><h2 id="my-plans-title">我的计划</h2></div>
        {!loading && <p>{plans.length ? "每一次出发，都留有下一次想念。" : "你的下一段旅程，会从这里开始。"}</p>}
      </div>
      {loading && <div className="journey-plan-skeletons" aria-label="正在加载历史行程">{[1, 2, 3].map(item => <i key={item} />)}</div>}
      {!loading && !plans.length && !error && <div className="journey-plans-empty">暂时还没有保存的行程，和小way聊聊下一次想去哪里吧。</div>}
      {!!plans.length && <div className="journey-plan-grid">
        {plans.map(trip => {
          const destination = trip.destination || "旅行";
          const days = dayCount(trip);
          return <button className="journey-plan-card" key={trip.id} onClick={() => onOpen?.(trip.id)} aria-label={`打开${destination}行程`}>
            <ItineraryCover className="journey-plan-photo" planId={trip.id} />
            <span className="journey-plan-body">
              <span className="journey-plan-meta"><i>{trip.parent_id ? `修改版 V${trip.version}` : "旅行计划"}</i><em>{days} 天</em></span>
              <strong>{destination}{days ? ` · ${days}日游` : ""}</strong>
              <small>{formatDates(trip) || "日期待定"}</small>
              <span className="journey-plan-open">查看行程 <UiIcon name="arrow-right" size={14} /></span>
            </span>
          </button>;
        })}
      </div>}
      {error && <div className="journey-plans-error" role="alert">{error}<button onClick={onRetry}>重新加载</button></div>}
      {hasMore && !error && <div ref={sentinelRef} className="journey-plans-sentinel" aria-live="polite">{loadingMore ? "正在展开更多计划…" : "继续向下浏览更多计划"}</div>}
      {!loading && !hasMore && plans.length > 6 && <div className="journey-plans-end">已经看到全部计划</div>}
    </section>
  );
}

function ActivityTimeline({
  items, currentUsername,
  onBriefUpdate, onBriefSubmit, onBriefDiscard,
  onRunCancel, onRunRetry, onRunOpen, onRunModify, onRunReply, onChatRetry,
  registerRunNode, onItineraryOpen,
}) {
  return items.map(item => {
    if (item.type === "message") {
      const message = item.entity;
      return (
        <article key={item.key} className={`chat-message ${message.role}`} aria-label={message.role === "user" ? "你的消息" : "小way的回复"}>
          <div className="chat-avatar" aria-hidden="true">{message.role === "user" ? currentUsername?.slice(-1) : <Mascot size={28} pose="idle" />}</div>
          <div className="chat-message-stack">
            <div className="chat-bubble">
              {message.role === "assistant"
                ? <ChatMessageContent content={message.content} />
                : message.content}
              {message.streaming && <span className="typing-caret" aria-hidden="true" />}
              {!message.streaming && <MessageArtifacts artifacts={message.artifacts} onOpen={onItineraryOpen} />}
              {!message.streaming && message.role === "assistant" && message.related_itinerary_id && (
                <div className="final-itinerary-actions">
                  <button onClick={() => onItineraryOpen?.(message.related_itinerary_id)}>打开完整行程</button>
                </div>
              )}
            </div>
          </div>
        </article>
      );
    }
    if (item.type === "brief") {
      return (
        <PlanningBriefCard
          key={item.key}
          brief={item.entity}
          onUpdate={onBriefUpdate}
          onSubmit={() => onBriefSubmit(item.entity)}
          onDiscard={() => onBriefDiscard(item.entity)}
        />
      );
    }
    if (item.type === "chat_thinking") {
      const isQueued = item.entity.status === "queued";
      const activity = item.entity.agent_activity;
      const activityLabel = activity?.label;
      return (
        <article key={item.key} className="chat-thinking" role="status" aria-live="polite">
          <div className="chat-avatar" aria-hidden="true"><Mascot size={28} pose="think" /></div>
          <div className="chat-thinking-body">
            <span className="chat-thinking-kicker">{isQueued ? "收到，正在接住这句话" : activityLabel ? "小way正在为你处理" : "小way正在思考"}</span>
            <strong>{isQueued ? "正在准备理解你的旅行想法…" : activityLabel || "正在梳理目的地、日期和你的偏好…"}</strong>
            <span className="chat-thinking-dots" aria-hidden="true"><i /><i /><i /></span>
          </div>
        </article>
      );
    }
    if (item.type === "chat_failure") {
      const run = item.entity;
      return (
        <section key={item.key} className="chat-understanding-failure" role="alert">
          <strong>这条消息暂时没有理解成功</strong>
          <p>{run.error_public?.message || "请重试"}</p>
          {run.error_public?.retryable !== false && (
            <button onClick={() => onChatRetry(run)}>重试这条消息</button>
          )}
        </section>
      );
    }
    const run = item.entity;
    return (
      <PlanningProgressTimeline
        key={item.key}
        refNode={node => registerRunNode(run.id, node)}
        run={run}
        onCancel={() => onRunCancel(run)}
        onRetry={() => onRunRetry(run)}
        onOpen={() => onRunOpen(run)}
        onModify={() => onRunModify(run)}
        onReply={() => onRunReply(run)}
      />
    );
  });
}

function PlanningProgressTimeline({ run, onCancel, onRetry, onOpen, onModify, onReply, refNode }) {
  const [expanded, setExpanded] = React.useState(true);
  const [now, setNow] = React.useState(Date.now());
  const [busy, setBusy] = React.useState("");
  const active = ["queued", "running", "waiting_user"].includes(run.status);
  React.useEffect(() => {
    if (!active) return undefined;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [active]);
  const started = Date.parse(run.created_at || run.queued_at || run.updated_at || "") || now;
  const seconds = Math.max(0, Math.floor((now - started) / 1000));
  const stageIndex = run.status === "succeeded" ? JOURNEY_STEPS.length
    : Math.max(0, Number(run.journey_step_index ?? 0));
  const stateTitle = run.status === "succeeded" ? `深度思考完成 · ${seconds || 1}s`
    : run.status === "waiting_user" ? (run.kind === "chat" ? "还需要你补充一点信息" : "规划需要你补充信息")
    : run.status === "failed" ? "这次规划暂未完成"
    : run.status === "cancelled" ? "规划已停止"
    : `正在深度思考… ${seconds}s`;
  const steps = JOURNEY_STEPS.map((step, index) => ({
    ...step,
    status: run.status === "succeeded" || index < stageIndex ? "done"
      : active && index === stageIndex ? "active" : "pending",
  }));
  const action = async (name, callback) => {
    if (busy) return;
    setBusy(name);
    try { await callback(); } finally { setBusy(""); }
  };
  return <section ref={refNode} data-run-id={run.id} className={`planning-thought status-${run.status}`} aria-live="polite">
    <button className="planning-thought-head" onClick={() => setExpanded(value => !value)} aria-expanded={expanded}>
      <span><i className={active ? "is-active" : ""} aria-hidden="true" />{stateTitle}</span><b aria-hidden="true"><UiIcon name={expanded ? "chevron-up" : "chevron-down"} size={16} /></b>
    </button>
    {run.status === "waiting_user" && run.pending_interaction?.question && (
      <p className="planning-thought-question">{run.pending_interaction.question}</p>
    )}
    {expanded && (run.kind !== "chat" || run.journey_step_index !== undefined) && <div className="planning-thought-steps" aria-label="规划进度">
      {steps.map((step, index) => <div key={step.key} className={`planning-thought-step ${step.status}`}>
        <i aria-hidden="true">{step.status === "done" ? <UiIcon name="check" size={11} strokeWidth={2.5} /> : step.status === "active" ? "•" : ""}</i>
        <div><strong>{step.label}</strong><small>{step.status === "active" && run.latest_progress_label ? run.latest_progress_label : step.status === "done" ? (step.doneDetail || "已完成") : step.detail}</small></div>
        {index < steps.length - 1 && <em aria-hidden="true" />}
      </div>)}
    </div>}
    {run.status === "waiting_user" && <span className="planning-thought-reply-hint">请在下方输入框回答</span>}
    {active && <button className="planning-thought-text-action" disabled={!!busy} onClick={() => action("cancel", onCancel)}>{busy === "cancel" ? "正在停止…" : "停止规划"}</button>}
    {["failed", "cancelled"].includes(run.status) && <button className="planning-thought-action" disabled={!!busy} onClick={() => action("retry", onRetry)}>{busy === "retry" ? "正在重试…" : "使用原需求再试一次"}</button>}
  </section>;
}

function MessageArtifacts({ artifacts, onOpen }) {
  const collections = (Array.isArray(artifacts) ? artifacts : [])
    .filter(item => item?.type === "itinerary_collection")
    .slice(0, 5);
  if (!collections.length) return null;
  return (
    <div className="message-artifacts" aria-label="保存的旅行方案">
      {collections.map((collection, collectionIndex) => (
        <section className="itinerary-collection" key={`${collection.type}-${collectionIndex}`}>
          <div className="itinerary-collection-head">
            <strong>{collection.title || "找到这些保存的方案"}</strong>
            {collection.match_kind === "near" && <span>相近结果</span>}
          </div>
          <div className="itinerary-card-grid">
            {(collection.items || []).slice(0, 5).map(card => (
              <button
                type="button"
                className="saved-itinerary-card"
                key={card.itinerary_id}
                onClick={() => onOpen?.(card.itinerary_id)}
                aria-label={`查看${card.destination || "旅行"}${card.duration_days ? `${card.duration_days}日` : ""}完整方案`}
              >
                <span className="saved-itinerary-kicker">
                  {card.is_modified ? `修改版 V${card.version}` : "保存的方案"}
                </span>
                <strong>{card.destination || "旅行方案"}{card.duration_days ? ` · ${card.duration_days}日` : ""}</strong>
                <small>{card.start_date && card.end_date ? `${card.start_date} — ${card.end_date}` : "日期未固定"}</small>
                {!!card.highlights?.length && <span className="saved-itinerary-highlights">{card.highlights.slice(0, 3).join(" · ")}</span>}
                <span className="saved-itinerary-open">查看完整方案 <UiIcon name="arrow-right" size={14} /></span>
              </button>
            ))}
          </div>
        </section>
      ))}
    </div>
  );
}

function ChatMessageContent({ content }) {
  const renderInline = (text, keyPrefix) => {
    const parts = String(text || "").split(/(\*\*[^*]+\*\*)/g);
    return parts.map((part, index) => {
      const match = part.match(/^\*\*(.+)\*\*$/);
      return match
        ? <strong key={`${keyPrefix}-${index}`}>{match[1]}</strong>
        : <React.Fragment key={`${keyPrefix}-${index}`}>{part}</React.Fragment>;
    });
  };

  const lines = String(content || "").split(/\r?\n/);
  return (
    <div className="chat-message-content">
      {lines.map((line, index) => {
        const trimmed = line.trim();
        if (!trimmed) return <span className="chat-message-space" key={index} aria-hidden="true" />;
        const numberedHeading = trimmed.match(/^\*\*(\d+\.\s+.+)\*\*$/);
        if (numberedHeading) {
          return <h4 key={index}>{numberedHeading[1]}</h4>;
        }
        if (/^[-•]\s+/.test(trimmed)) {
          return (
            <div className="chat-message-list-item" key={index}>
              <span aria-hidden="true">•</span>
              <p>{renderInline(trimmed.replace(/^[-•]\s+/, ""), `line-${index}`)}</p>
            </div>
          );
        }
        return <p key={index}>{renderInline(trimmed, `line-${index}`)}</p>;
      })}
    </div>
  );
}

function PlanningBriefCard({ brief, onSubmit }) {
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState("");
  if (brief.status !== "ready") return null;
  const data = brief.data || {};
  const constraints = data.trip_constraints || [];
  const first = category => constraints.find(item => item.category === category)?.value_text;
  const details = [
    ["location", "目的地", data.destination || "待确认"],
    ["clock", "日期", data.start_date && data.end_date ? `${data.start_date.slice(5)} — ${data.end_date.slice(5)}` : "待确认"],
    ["navigation", "节奏", first("travel_pace") || data.habit_preference || "舒适均衡"],
    ["heart", "同行", first("companion_context") || "同行情况待确认"],
    ["navigation", "景点间交通预算", data.transport_budget ? `${data.transport_budget}（计入总预算）` : "未设费用上限"],
  ];
  const tags = constraints
    .filter(item => !["travel_pace", "companion_context"].includes(item.category))
    .map(item => item.value_text).filter(Boolean).slice(0, 3);
  const submit = async () => {
    if (busy) return;
    setBusy(true); setError("");
    try { await onSubmit(brief); }
    catch (e) { setError(e.message || "创建规划任务失败，请重试"); }
    finally { setBusy(false); }
  };
  return <section className="brief-confirmation" aria-labelledby={`brief-title-${brief.id}`}>
    <div className="brief-confirmation-head"><div><span>规划确认</span><h3 id={`brief-title-${brief.id}`}>必要条件已齐全</h3></div><b aria-hidden="true"><UiIcon name="sparkle" size={18} /></b></div>
    <div className="brief-confirmation-grid">{details.map(([icon, label, value]) => <div key={label}><i aria-hidden="true"><UiIcon name={icon} size={19} /></i><p><small>{label}</small><strong>{value}</strong></p></div>)}</div>
    {!!tags.length && <div className="brief-confirmation-tags">{tags.map(tag => <span key={tag}>{tag}</span>)}</div>}
    {error && <p className="brief-error" role="alert">{error}</p>}
    <button className="brief-confirmation-submit" disabled={busy} onClick={submit}>{!busy && <UiIcon name="sparkle" size={17} />}{busy ? "正在创建规划…" : "开始规划"}</button>
  </section>;
}

/* ── 行程详情页 ───────────────────────────────── */
function DetailLightRays() {
  const rays = [
    [8, -22, 190, 1.1, -2.3, 15.4, .72],
    [23, -11, 255, 1.8, -8.6, 12.8, .88],
    [38, 7, 215, 1.25, -5.1, 17.1, .67],
    [52, -5, 300, 2.1, -10.2, 13.7, .96],
    [67, 18, 230, 1.45, -3.8, 16.2, .76],
    [81, -16, 275, 1.7, -7.4, 14.6, .82],
    [93, 24, 180, 1.05, -1.2, 18.1, .64],
  ];
  return <div className="detail-light-rays" aria-hidden="true">
    <div className="detail-light-rays-inner">
      <i className="detail-light-rays-glow left" />
      <i className="detail-light-rays-glow right" />
      {rays.map(([left, rotate, width, swing, delay, cycle, intensity], index) => <i key={index} className="detail-light-ray" style={{
        "--ray-left": `${left}%`, "--ray-rotate": `${rotate}deg`, "--ray-width": `${width}px`,
        "--ray-swing": `${swing}deg`, "--ray-delay": `${delay}s`, "--ray-cycle": `${cycle}s`, "--ray-intensity": intensity,
      }} />)}
    </div>
  </div>;
}

function DetailWeatherCard({ destination, weather }) {
  if (!weather) return null;
  const current = weather.hi !== "—" ? weather.hi : weather.lo;
  return <aside className="detail-weather-card" aria-label={`${destination}天气：${weather.text}`}>
    <div className="detail-weather-main">
      <p>{destination || "目的地"}</p>
      <div className="detail-weather-hero"><WeatherGlyph value={weather.icon || weather.text} size={30} /><strong>{current}°</strong></div>
      <small>{weather.text || "天气信息"}</small>
    </div>
    <div className="detail-weather-range">
      <span className="high"><UiIcon name="arrow-up" size={13} />{weather.hi}°</span>
      <i />
      <span className="low"><UiIcon name="arrow-up" size={13} />{weather.lo}°</span>
    </div>
  </aside>;
}

function itemDuration(item) {
  if (!item?.start || !item?.end) return item?.type === "attraction" ? "建议停留" : "用餐";
  const [sh, sm] = item.start.split(":").map(Number);
  const [eh, em] = item.end.split(":").map(Number);
  const minutes = (eh * 60 + em) - (sh * 60 + sm);
  if (!(minutes > 0)) return `${item.start}–${item.end}`;
  if (minutes % 60 === 0) return `${minutes / 60} 小时`;
  return `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分`;
}

function ReferenceDayTimeline({ items, onNav, activeNavKey, tipStatus }) {
  return <div className="reference-itinerary-timeline">
    {(items || []).map((item, index) => {
      const next = items[index + 1];
      const navKey = next ? `reference:${index}` : null;
      const isMeal = item.type !== "attraction";
      const note = item.note || item.reason || item.address || item.addr
        || (tipStatus === "queued" || tipStatus === "running" ? "小way正在整理这处地点的旅行贴士…" : "已同步到当前路线。点开地图标记可查看更多地点信息。");
      const card = <article className="reference-timeline-card">
        <div className="reference-timeline-card-row">
          <time>{item.start || (item.type === "lunch" ? "午餐" : item.type === "dinner" ? "晚餐" : "待定")}</time>
          <div className="reference-timeline-main">
            <div className="reference-timeline-title"><h3>{item.name || "待安排地点"}</h3><span>{itemDuration(item)}</span></div>
            <p>{note}</p>
            <small><UiIcon name="clock" size={13} />{isMeal ? "建议预留用餐时间" : `建议停留 ${itemDuration(item)}`}</small>
          </div>
          <UiIcon name="arrow-right" size={16} className="reference-timeline-chevron" />
        </div>
      </article>;
      return <div className={`reference-timeline-stop${isMeal ? " is-meal" : ""}`} key={`${item.name}-${index}`}>
        <span className="reference-timeline-node"><UiIcon name={isMeal ? "utensils" : "location"} size={14} /></span>
        {index === 0 ? <div className="reference-border-trail">{card}</div> : card}
        {next && <div className="reference-transfer">
          <button className={activeNavKey === navKey ? "active" : ""} onClick={() => {
            if (!item.location || !next.location) return;
            onNav?.(activeNavKey === navKey ? null : navKey, { from: item.location, to: next.location });
          }} disabled={!item.location || !next.location}>
            <UiIcon name="navigation" size={13} /><b>前往下一站</b>
            <span>{next.dist ? `${next.dist} km` : "查看地图路线"}</span>
          </button>
        </div>}
      </div>;
    })}
  </div>;
}

function LegacyTripDetailPage({ plan: planProp, planId: planIdProp, onRequestModify, currentUsername, onBack, onPlanChange, initialEditing = false, onExitEditing }) {
  const [plan, setPlan] = React.useState(planProp);
  const [planId, setPlanId] = React.useState(planIdProp);
  const [dayIdx, setDayIdx] = React.useState(0);
  const [optimizedDays, setOptimizedDays] = React.useState({});
  const [optimizingDay, setOptimizingDay] = React.useState(null);
  const [dayMsg, setDayMsg] = React.useState(null);
  const [hotel, setHotel] = React.useState("");
  const [notes, setNotes] = React.useState("");
  const [metaDirty, setMetaDirty] = React.useState(false);
  const [metaSaving, setMetaSaving] = React.useState(false);
  const [activeNavKey, setActiveNavKey] = React.useState(null);
  const [activeNavPair, setActiveNavPair] = React.useState(null);
  const [nearbyTarget, setNearbyTarget] = React.useState(null);
  const [themeInput, setThemeInput] = React.useState("");
  const [mapWidth, setMapWidth] = React.useState(58);
  const [resizingPane, setResizingPane] = React.useState(false);
  const [sheetHeight, setSheetHeight] = React.useState(39);
  const [resizingSheet, setResizingSheet] = React.useState(false);
  const sheetDragActive = React.useRef(false);
  const [sheetCollapsed, setSheetCollapsed] = React.useState(false);
  const [historyOpen, setHistoryOpen] = React.useState(false);
  const [historyItems, setHistoryItems] = React.useState([]);
  const [historyLoading, setHistoryLoading] = React.useState(false);
  const [shareNotice, setShareNotice] = React.useState("");
  const saveScopeRef = React.useRef(null);
  React.useEffect(() => {
    saveScopeRef.current = {};
    return () => { saveScopeRef.current = null; };
  }, [planIdProp, currentUsername]);

  React.useEffect(() => {
    setPlan(planProp);
    setPlanId(planIdProp);
    setDayIdx(0);
    setOptimizedDays({});
    setOptimizingDay(null);
    setDayMsg(null);
    setHotel(planProp?.hotel || "");
    setNotes(planProp?.notes || "");
    setMetaDirty(false);
    setActiveNavKey(null);
    setActiveNavPair(null);
    setNearbyTarget(null);
    // 切换行程时清除编辑态，防止残留
    setEditing(false);
    setDraft(null); draftRef.current = null;
    setUndoStack([]);
    setRedoStack([]);
    setSaveErr("");
    setSearchTarget(null);
    setSheetHeight(39);
    setSheetCollapsed(false);
    setHistoryOpen(false);
    setShareNotice("");
  }, [planProp, planIdProp]);

  const clampMapWidth = React.useCallback(value => {
    const viewport = Math.max(window.innerWidth || 0, 1);
    const maxWithChatRoom = ((viewport - 220) / viewport) * 100;
    return Math.max(22, Math.min(84, maxWithChatRoom, value));
  }, []);

  const resizeMapAt = React.useCallback(clientX => {
    const raw = ((window.innerWidth - clientX) / window.innerWidth) * 100;
    setMapWidth(clampMapWidth(raw));
  }, [clampMapWidth]);

  const clampSheetHeight = React.useCallback(value => {
    const viewport = Math.max(window.innerHeight || 0, 1);
    const minimum = Math.min(39, (340 / viewport) * 100);
    return Math.max(minimum, Math.min(82, value));
  }, []);

  const resizeSheetAt = React.useCallback(clientY => {
    const raw = ((window.innerHeight - clientY) / Math.max(window.innerHeight, 1)) * 100;
    setSheetHeight(clampSheetHeight(raw));
  }, [clampSheetHeight]);

  React.useEffect(() => {
    const move = event => {
      if (sheetDragActive.current) resizeSheetAt(event.clientY);
    };
    const stop = () => {
      if (!sheetDragActive.current) return;
      sheetDragActive.current = false;
      setResizingSheet(false);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", stop);
    window.addEventListener("pointercancel", stop);
    return () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", stop);
      window.removeEventListener("pointercancel", stop);
    };
  }, [resizeSheetAt]);

  const showHistory = async () => {
    const nextOpen = !historyOpen;
    setHistoryOpen(nextOpen);
    if (!nextOpen || !planId) return;
    setHistoryLoading(true);
    try { setHistoryItems(await listConversations(planId)); }
    catch { setHistoryItems([]); }
    finally { setHistoryLoading(false); }
  };

  const sharePlan = async () => {
    if (!planId) return;
    const url = `${window.location.origin}/?view_plan_id=${encodeURIComponent(planId)}`;
    try {
      if (navigator.share) await navigator.share({ title: plan?.title || "旅行行程", url });
      else { await navigator.clipboard.writeText(url); setShareNotice("链接已复制"); }
    } catch (error) {
      if (error?.name !== "AbortError") setShareNotice("暂时无法分享");
    }
    window.setTimeout(() => setShareNotice(""), 2200);
  };

  const openConversationResult = React.useCallback(async resultPlanId => {
    if (!resultPlanId) return;
    try {
      const data = await getHistoryItem(resultPlanId);
      if (!data?.plan) return;
      const adapted = adaptPlan(data.plan, currentUsername);
      setPlan(adapted);
      setPlanId(resultPlanId);
      setDayIdx(0);
      onPlanChange?.(data.plan, resultPlanId);
    } catch {}
  }, [currentUsername, onPlanChange]);

  // 详情页自持 itinerary 级订阅；聊天页卸载后仍可原位取得贴士。
  React.useEffect(() => {
    if (!planId) return undefined;
    let abort = null;
    let alive = true;
    streamItineraryTips(planId, {
      onAbort: fn => { abort = fn; },
      onEvent: event => {
        if (event.payload?.kind !== "itinerary.tip_status_changed") return;
        getHistoryItem(planId).then(data => {
          if (!alive || !data?.plan) return;
          const adapted = adaptPlan(data.plan, currentUsername);
          setPlan(previous => ({ ...adapted, logs: previous?.logs || adapted.logs }));
        }).catch(() => {});
      },
    });
    return () => { alive = false; abort?.(); };
  }, [planId, currentUsername]);

  const applyDayTimeline = (dayI, timeline) => {
    const raw = { ...plan._raw, days: plan._raw.days.map((d, i) => i === dayI ? { ...d, timeline } : d) };
    const adapted = adaptPlan(raw, currentUsername);
    adapted.logs = plan.logs;
    setPlan(adapted);
  };

  const showDayMsg = (dayNo, text) => {
    setDayMsg({ day: dayNo, text });
    setTimeout(() => setDayMsg(m => (m && m.day === dayNo && m.text === text ? null : m)), 5000);
  };

  const handleOptimize = async (dayNo) => {
    if (!confirm("将按最短路程优化景点游玩顺序，餐厅需要你重新规划")) return;
    setOptimizingDay(dayNo);
    try {
      const dayI = dayNo - 1;
      const rawTimeline = plan._raw?.days?.[dayI]?.timeline;
      const res = await optimizeDay(planId, dayNo);
      if (res.optimized_day) {
        if (res.improved) {
          setOptimizedDays(prev => ({ ...prev, [dayI]: rawTimeline }));
          showDayMsg(dayNo, `已优化：${res.original_km}km → ${res.optimized_km}km`);
        } else {
          showDayMsg(dayNo, "当前顺序已是最短路线");
        }
        applyDayTimeline(dayI, res.optimized_day.timeline);
      }
    } catch (e) { alert(e.message || "优化失败"); }
    finally { setOptimizingDay(null); }
  };

  const handleNav = (key, pair, nearbyItem) => {
    if (nearbyItem) {
      setNearbyTarget({ location: nearbyItem.location, name: nearbyItem.name });
      return;
    }
    if (key === activeNavKey) {
      setActiveNavKey(null); setActiveNavPair(null);
    } else {
      setActiveNavKey(key); setActiveNavPair(pair);
    }
  };

  const handleSaveMeta = async () => {
    setMetaSaving(true);
    try {
      await savePlanMetadata(planId, { hotel, notes });
      setMetaDirty(false);
    } catch (e) { alert(e.message || "保存失败"); }
    finally { setMetaSaving(false); }
  };

  const handleRevert = async (dayNo) => {
    const dayI = dayNo - 1;
    const orig = optimizedDays[dayI];
    if (!orig || !planId) return;
    try {
      await revertDay(planId, dayNo, orig);
      setOptimizedDays(prev => { const n = { ...prev }; delete n[dayI]; return n; });
      applyDayTimeline(dayI, orig);
      showDayMsg(dayNo, "已回退到优化前的顺序");
    } catch (e) { alert(e.message || "回退失败"); }
  };

  // ── 手动编辑态 ──
  const [editing, setEditing] = React.useState(false);
  const [draft, setDraft] = React.useState(null);          // _raw.days 的深拷贝
  const draftRef = React.useRef(null);
  React.useEffect(() => { draftRef.current = draft; }, [draft]);
  const [undoStack, setUndoStack] = React.useState([]);    // 元素 = draft 快照
  const [redoStack, setRedoStack] = React.useState([]);
  const [editVer, setEditVer] = React.useState(0);         // 每次变更 +1，驱动 Sortable 重挂载
  const [saving, setSaving] = React.useState(false);
  const [saveErr, setSaveErr] = React.useState("");
  // 搜索弹层目标：{ dayI, idx } 替换；{ dayI, idx:null, addType } 新增
  const [searchTarget, setSearchTarget] = React.useState(null);

  const dirty = undoStack.length > 0;
  const dirtyRef = React.useRef(false);
  React.useEffect(() => { dirtyRef.current = dirty; }, [dirty]);

  // 编辑中刷新/关页守卫（dirtyRef 避免 beforeunload 闭包捕获过期 dirty 值）
  React.useEffect(() => {
    if (!editing) return;
    const guard = (e) => { if (dirtyRef.current) { e.preventDefault(); e.returnValue = ""; } };
    window.addEventListener("beforeunload", guard);
    return () => window.removeEventListener("beforeunload", guard);
  }, [editing]);

  const enterEdit = () => {
    const clone = structuredClone(plan._raw.days);
    setDraft(clone); draftRef.current = clone;
    setUndoStack([]); setRedoStack([]); setSaveErr("");
    setEditing(true); setEditVer(v => v + 1);
  };

  React.useEffect(() => { if (initialEditing) enterEdit(); }, [initialEditing, planProp, planIdProp]);

  const exitEdit = () => {
    if (dirty && !confirm("放弃未保存的修改？")) return;
    setEditing(false); setDraft(null); draftRef.current = null; setSaveErr(""); setSearchTarget(null);
    onExitEditing?.();
  };

  // 所有编辑操作的唯一入口：拷贝 → 变更 → 重算距离 → 压撤销栈
  // 经 draftRef 读最新值，同一 tick 多次调用也不会丢快照
  const applyEdit = (mutate) => {
    const cur = draftRef.current;
    setUndoStack(s => [...s, cur]);
    setRedoStack([]);
    const next = structuredClone(cur);
    mutate(next);
    next.forEach(d => recalcDayDists(d.timeline));
    setDraft(next);
    draftRef.current = next;
    setEditVer(v => v + 1);
  };

  const undo = () => {
    if (!undoStack.length) return;
    const cur = draftRef.current;
    const prev = undoStack[undoStack.length - 1];
    setRedoStack(r => [...r, cur]);
    setUndoStack(s => s.slice(0, -1));
    setDraft(prev);
    draftRef.current = prev;
    setEditVer(v => v + 1);
  };

  const redo = () => {
    if (!redoStack.length) return;
    const cur = draftRef.current;
    const next = redoStack[redoStack.length - 1];
    setUndoStack(s => [...s, cur]);
    setRedoStack(r => r.slice(0, -1));
    setDraft(next);
    draftRef.current = next;
    setEditVer(v => v + 1);
  };

  const saveEdit = async () => {
    const scope = saveScopeRef.current;
    setSaving(true); setSaveErr("");
    try {
      const days = draftRef.current.map((d, i) => ({ day: d.day ?? i + 1, timeline: d.timeline }));
      const res = await saveTimeline(planId, days);
      if (saveScopeRef.current !== scope) return;
      let savedPlan = res.plan;
      // 保存主题编辑
      const day_themes = {};
      draftRef.current.forEach((d, i) => { if (typeof d.theme === "string") day_themes[String(d.day ?? i + 1)] = d.theme; });
      if (Object.keys(day_themes).length) {
        try {
          const metadata = await savePlanMetadata(planId, { day_themes });
          if (saveScopeRef.current !== scope) return;
          savedPlan = metadata?.plan || {
            ...savedPlan,
            days: savedPlan.days.map(day => Object.hasOwn(day_themes, String(day.day))
              ? { ...day, theme: day_themes[String(day.day)] } : day),
          };
        } catch { throw new Error("路线已保存，主题保存失败，请重试。"); }
      }
      const adapted = adaptPlan(savedPlan, currentUsername);
      adapted.logs = plan.logs;
      // 手动路线编辑改变了指纹；旧版本贴士不应继续作为当前路线的内容展示。
      adapted.tip_status = "unavailable";
      setPlan(adapted);
      setEditing(false); setDraft(null); draftRef.current = null; setSearchTarget(null);
      setOptimizedDays({});   // 手动编辑后旧的"优化前快照"失效
      if (onExitEditing) onPlanChange?.(savedPlan, planId);
    } catch (e) {
      if (saveScopeRef.current !== scope) return;
      // 网络层错误（Failed to fetch 等）对用户不可读，换成中文提示
      const msg = e.message && !/fetch|network/i.test(e.message) ? e.message : "保存失败，请检查网络后重试";
      setSaveErr(msg);
    } finally { if (saveScopeRef.current === scope) setSaving(false); }
  };

  // ── 各编辑操作 ──
  const handleReorder = (from, to) =>
    applyEdit(d => { d[dayIdx].timeline = reorderKeepTimes(d[dayIdx].timeline, from, to); });

  const handleDelete = (idx) =>
    applyEdit(d => { d[dayIdx].timeline.splice(idx, 1); });

  const handleTimeChange = (idx, st, et) =>
    applyEdit(d => { Object.assign(d[dayIdx].timeline[idx], { start_time: st, end_time: et }); });

  const handlePoiPick = (poi) => {
    const { dayI, idx, addType } = searchTarget;
    setSearchTarget(null);
    applyEdit(d => {
      const tl = d[dayI].timeline;
      if (idx != null) {
        const old = tl[idx];
        if (old.type === "attraction") {
          // 继承时间段与时段，其余字段来自新 POI；旧贴士不再适用
          tl[idx] = { ...old, name: poi.name, rating: poi.rating ?? null,
            open_time: poi.open_time ?? null, location: poi.location,
            address: poi.address ?? null, photo: poi.photo ?? null, photos: Array.isArray(poi.photos) ? poi.photos : [], poi_id: poi.id || poi.poi_id || null, tip: null };
        } else {
          tl[idx] = { type: old.type, name: poi.name, rating: poi.rating ?? null,
            cost: poi.cost ?? null, address: poi.address ?? null,
            location: poi.location, photo: poi.photo ?? null, photos: Array.isArray(poi.photos) ? poi.photos : [], poi_id: poi.id || poi.poi_id || null,
            reason: null, no_restaurant: false };
        }
      } else if (addType === "attraction") {
        tl.push({ type: "attraction", name: poi.name, rating: poi.rating ?? null,
          open_time: poi.open_time ?? null, location: poi.location,
          photo: poi.photo ?? null, photos: Array.isArray(poi.photos) ? poi.photos : [], poi_id: poi.id || poi.poi_id || null, tip: null,
          start_time: null, end_time: null, period: "afternoon" });
      } else {
        tl.push({ type: addType, name: poi.name, rating: poi.rating ?? null,
          cost: poi.cost ?? null, address: poi.address ?? null,
          location: poi.location, photo: poi.photo ?? null, photos: Array.isArray(poi.photos) ? poi.photos : [], poi_id: poi.id || poi.poi_id || null,
          reason: null, no_restaurant: false });
      }
    });
  };

  // 编辑态主题 input 同步
  React.useEffect(() => {
    if (editing && draft) setThemeInput(draft[dayIdx]?.theme || "");
  }, [editing, dayIdx, editVer]); // eslint-disable-line

  // 编辑态视图：draft 经 adaptPlan 渲染（地图点位/items 跟随编辑实时刷新）
  const editedView = React.useMemo(() => {
    if (!editing || !draft) return null;
    const adapted = adaptPlan({ ...plan._raw, days: draft }, currentUsername);
    adapted.logs = plan.logs;
    return adapted;
  }, [editing, editVer]); // editVer 是版本令牌：draft 每次变更都 +1，故意省略 draft/plan 直接依赖

  const startModify = (query) => {
    if (editing) {
      if (!confirm("正在手动编辑，离开将丢弃未保存的修改。继续？")) return;
      setEditing(false); setDraft(null); draftRef.current = null; setSaveErr("");
    }
    const content = String(query || "").trim();
    if (!content || !planId) return;
    onRequestModify?.(content, planId);
  };

  const retryTips = async () => {
    if (!planId) return;
    try {
      const result = await retryItineraryTips(planId);
      if (result.status === "queued") setPlan(previous => ({ ...previous, tip_status: "queued" }));
      if (result.status === "succeeded") {
        const data = await getHistoryItem(planId);
        if (data?.plan) setPlan(adaptPlan(data.plan, currentUsername));
      }
    } catch (e) { alert(e.message || "贴士生成任务提交失败"); }
  };

  if (!plan) return null;
  const viewPlan = editing && editedView ? editedView : plan;
  const day = viewPlan.days[dayIdx];
  const dayNo = dayIdx + 1;
  const hasAttractions = (day.items || []).filter(it => it.type === "attraction").length >= 2;
  const isOptimized = optimizedDays[dayIdx] !== undefined;

  const handlePickMeal = async (poi, mealType) => {
    if (!planId) return;
    setNearbyTarget(null);
    const rawTimeline = plan._raw.days[dayIdx]?.timeline || [];
    const mealEntry = {
      type: mealType,
      name: poi.name,
      rating: poi.rating ?? null,
      cost: poi.cost ?? null,
      address: poi.address ?? null,
      location: poi.location ?? null,
      photo: poi.photo ?? null, photos: Array.isArray(poi.photos) ? poi.photos : [], poi_id: poi.id || poi.poi_id || null,
      open_time: poi.open_time ?? null,
      tel: poi.tel ?? null,
      reason: null,
      no_restaurant: false,
    };
    const hasSlot = rawTimeline.some(it => it.type === mealType);
    const newTimeline = hasSlot
      ? rawTimeline.map(item => item.type !== mealType ? item : mealEntry)
      : [...rawTimeline, mealEntry];
    try {
      await saveTimeline(planId, [{ day: dayIdx + 1, timeline: newTimeline }]);
      applyDayTimeline(dayIdx, newTimeline);
      showDayMsg(dayNo, `已更新${mealType === "lunch" ? "午餐" : "晚餐"}：${poi.name}`);
    } catch (e) {
      alert(e.message || "更新失败");
    }
  };

  return <React.Fragment>
    {searchTarget && <PoiSearchModal
      city={plan.destination}
      kind={searchTarget.idx != null
        ? (draft[searchTarget.dayI].timeline[searchTarget.idx].type === "attraction" ? "attraction" : "restaurant")
        : (searchTarget.addType === "attraction" ? "attraction" : "restaurant")}
      title={searchTarget.idx != null ? "更换为…" : "添加…"}
      onPick={handlePoiPick} onClose={() => setSearchTarget(null)} />}
    {nearbyTarget && <NearbySearchModal location={nearbyTarget.location} name={nearbyTarget.name}
      onClose={() => setNearbyTarget(null)} onPickMeal={handlePickMeal} />}
    <div className="page page-fade trip-detail-page">
      <div className={`detail-workspace${resizingPane ? " is-resizing" : ""}${resizingSheet ? " is-sheet-resizing" : ""}`} style={{ "--detail-map-width": `${mapWidth}%` }}>
        <ItineraryCopilot plan={plan} planId={planId} day={day} dayIdx={dayIdx} editing={editing}
          currentUsername={currentUsername} onBack={() => { if (!dirty || confirm("放弃未保存的修改？")) onBack?.(); }} onHistory={showHistory} onShare={sharePlan}
          historyOpen={historyOpen} historyItems={historyItems} historyLoading={historyLoading}
          shareNotice={shareNotice} onPlanResult={openConversationResult} />

        <main className={`detail-map-overlay${sheetCollapsed ? " sheet-collapsed" : ""}`} style={{ width: `${mapWidth}%` }}>
          <div className="detail-map-resizer" role="separator" tabIndex="0" aria-label="调整聊天与地图宽度"
            aria-orientation="vertical" aria-valuemin="22" aria-valuemax="84" aria-valuenow={Math.round(mapWidth)}
            onPointerDown={event => { event.preventDefault(); event.currentTarget.setPointerCapture?.(event.pointerId); setResizingPane(true); resizeMapAt(event.clientX); }}
            onPointerMove={event => { if (event.currentTarget.hasPointerCapture?.(event.pointerId)) resizeMapAt(event.clientX); }}
            onPointerUp={event => { event.currentTarget.releasePointerCapture?.(event.pointerId); setResizingPane(false); }}
            onPointerCancel={() => setResizingPane(false)} onDoubleClick={() => setMapWidth(58)}
            onKeyDown={event => {
              if (!["ArrowLeft", "ArrowRight", "Home"].includes(event.key)) return;
              event.preventDefault();
              if (event.key === "Home") setMapWidth(58);
              else setMapWidth(value => clampMapWidth(value + (event.key === "ArrowLeft" ? 2 : -2)));
            }} />
          <section className="detail-map-zone" aria-label={`Day ${dayNo} 路线地图`}>
            <MapPanel day={day} dayIdx={dayIdx} navPair={activeNavPair} workbenchControls
              onNavClear={() => { setActiveNavKey(null); setActiveNavPair(null); }} />
            <DetailWeatherCard destination={plan.destination} weather={plan.weather?.[dayIdx] || plan.weather?.[0]} />
          </section>

          <section className={`detail-itinerary-sheet${sheetCollapsed ? " collapsed" : ""}`} aria-label={`Day ${dayNo} 行程详情`}
            style={sheetCollapsed ? undefined : { height: `${sheetHeight}%` }}>
            <div className="detail-sheet-resizer" role="separator" tabIndex="0" aria-label="调整地图与行程详情高度"
              aria-orientation="horizontal" aria-valuemin="39" aria-valuemax="82" aria-valuenow={Math.round(sheetHeight)}
              onPointerDown={event => {
                event.preventDefault();
                sheetDragActive.current = true;
                setSheetCollapsed(false);
                setResizingSheet(true);
                resizeSheetAt(event.clientY);
              }}
              onDoubleClick={() => { sheetDragActive.current = false; setResizingSheet(false); setSheetCollapsed(false); setSheetHeight(39); }}
              onKeyDown={event => {
                if (!["ArrowUp", "ArrowDown", "Home"].includes(event.key)) return;
                event.preventDefault();
                setSheetCollapsed(false);
                if (event.key === "Home") setSheetHeight(39);
                else setSheetHeight(value => clampSheetHeight(value + (event.key === "ArrowUp" ? 4 : -4)));
              }}><span /></div>
            <nav className="detail-fluid-tabs" aria-label="选择行程日期"
              style={{ "--active-day": dayIdx, "--day-count": plan.days.length }}>
              <div className="detail-day-tabs" role="tablist" onKeyDown={event => {
                if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
                const tabs = [...event.currentTarget.querySelectorAll('[role="tab"]')];
                const current = Math.max(0, tabs.indexOf(document.activeElement));
                const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1
                  : (current + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
                event.preventDefault();
                tabs[next]?.focus();
              }}>
                <i className="detail-fluid-indicator" aria-hidden="true" />
                {plan.days.map((_, index) => <button key={index} role="tab" aria-selected={index === dayIdx}
                  className={index === dayIdx ? "active" : ""}
                  onClick={() => { setDayIdx(index); setActiveNavKey(null); setActiveNavPair(null); }}>
                  <b>Day {index + 1}</b>
                </button>)}
              </div>
            </nav>
            <div className="detail-shift-tabs">
              <button className={sheetCollapsed ? "is-collapsed" : ""} aria-expanded={!sheetCollapsed}
                onClick={() => setSheetCollapsed(value => !value)}>
                <span><UiIcon name="chevron-down" size={15} />{sheetCollapsed ? "展开" : "收起"}</span>
              </button>
            </div>

            <div className="detail-sheet-content">
              <div className="detail-day-view" style={{ "--day-color": ["#2cb8f5", "#ff9650", "#6c59ef"][dayIdx % 3] }}>
                <div className="detail-day-overview">
                  <div><strong>{day.theme || `Day ${dayNo} 的旅行安排`}</strong><p><span>{day.items.length} 个安排</span><i>·</i><span>{day.date || plan.date_range}</span><i>·</i><span>地图已同步</span></p></div>
                  <span className="detail-route-badge"><i />{day.items.filter(item => item.type === "attraction").length} 个景点 · 当前路线</span>
                </div>

                {editing ? <div className="detail-editing-zone">
                  <EditToolbar canUndo={undoStack.length > 0} canRedo={redoStack.length > 0}
                    saving={saving} saveErr={saveErr} onUndo={undo} onRedo={redo} onCancel={exitEdit} onSave={saveEdit} />
                  <EditableTimeline rawTimeline={draft[dayIdx].timeline} ver={`${dayIdx}-${editVer}`} allowMeals={!plan._raw?.scoring_profile}
                    onReorder={handleReorder} onReplace={idx => setSearchTarget({ dayI: dayIdx, idx })}
                    onDelete={handleDelete} onTimeChange={handleTimeChange}
                    onAdd={addType => setSearchTarget({ dayI: dayIdx, idx: null, addType })}
                    onDropCandidate={(idx, candidate) => applyEdit(days => {
                      const old = days[dayIdx].timeline[idx];
                      days[dayIdx].timeline[idx] = { ...old, name: candidate.name, rating: candidate.rating ?? null,
                        open_time: candidate.open_time ?? null, location: candidate.location, photo: candidate.photo ?? null,
                        photos: Array.isArray(candidate.photos) ? candidate.photos : [], poi_id: candidate.id || candidate.poi_id || null,
                        address: candidate.address ?? null, tip: null };
                    })} />
                </div> : <ReferenceDayTimeline items={day.items} onNav={handleNav} activeNavKey={activeNavKey} tipStatus={plan.tip_status} />}

                <details className="detail-more-panel" open={editing || undefined}>
                  <summary>更多行程信息与编辑</summary>
                  <div className="detail-more-actions">
                    {!editing && planId && <button onClick={enterEdit}><UiIcon name="edit" size={14} />编辑行程</button>}
                    {!editing && hasAttractions && planId && (isOptimized
                      ? <button onClick={() => handleRevert(dayNo)}><UiIcon name="undo" size={14} />回退优化</button>
                      : <button disabled={optimizingDay === dayNo} onClick={() => handleOptimize(dayNo)}><UiIcon name="shuffle" size={14} />{optimizingDay === dayNo ? "优化中…" : "优化路线"}</button>)}
                    {dayMsg && dayMsg.day === dayNo && <span>{dayMsg.text}</span>}
                  </div>
                  {editing && <input className="theme-input" value={themeInput} onChange={event => setThemeInput(event.target.value)}
                    onBlur={() => { const current = draft[dayIdx]?.theme || ""; if (themeInput !== current) applyEdit(days => { days[dayIdx].theme = themeInput; }); }}
                    onKeyDown={event => event.key === "Enter" && event.target.blur()} />}
                  <RecommendStrip candidates={viewPlan.candidate_spots} editing={editing} />
                  <div className="tip-card"><Mascot size={72} pose="point" /><div className="tip-body"><div className="tip-title">小way的小贴士</div>
                    {plan.tips.length ? <ul className="tip-list">{plan.tips.map((tip, index) => <li key={index}>{tip}</li>)}</ul> : <div className="tip-text">行程已为你精心安排，祝旅途愉快！</div>}
                    {["failed", "cancelled", "unavailable"].includes(plan.tip_status) && planId && <button className="nearby-btn" onClick={retryTips}>重新生成景点贴士</button>}
                  </div></div>
                  {planId && <div className="hotel-notes-section"><div className="hn-field"><label className="hn-label"><UiIcon name="bed" size={15} />住宿</label><input className="hn-input" value={hotel} placeholder="记录酒店名称、价格…" onChange={event => { setHotel(event.target.value); setMetaDirty(true); }} /></div>
                    <div className="hn-field"><label className="hn-label"><UiIcon name="edit" size={15} />备注</label><textarea className="hn-input hn-textarea" value={notes} rows="2" placeholder="特别要求、注意事项…" onChange={event => { setNotes(event.target.value); setMetaDirty(true); }} /></div>
                    {metaDirty && <button className="go-btn" disabled={metaSaving} onClick={handleSaveMeta}>{metaSaving ? "保存中…" : "保存行程备注"}</button>}
                  </div>}
                  {plan.logs?.length > 0 && <details className="log-details"><summary>规划过程日志（{plan.logs.length} 步）</summary><ul className="log-list">{plan.logs.map((log, index) => <li key={index}>{log}</li>)}</ul></details>}
                </details>
              </div>
            </div>
          </section>
        </main>
      </div>
    </div>
  </React.Fragment>;
}

function ItineraryCopilot({
  plan, planId, day, dayIdx, editing, currentUsername, onBack,
  onHistory, onShare, historyOpen, historyItems, historyLoading,
  shareNotice, onPlanResult,
}) {
  const [query, setQuery] = React.useState("");
  const [activeId, setActiveId] = React.useState(null);
  const [state, setState] = React.useState(() => ChatState.initialState());
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState("");
  const abortsRef = React.useRef({});
  const scrollRef = React.useRef(null);

  const runs = Object.values(state.runs || {}).sort((a, b) => String(a.created_at || "").localeCompare(String(b.created_at || "")));
  const blockingRun = [...runs].reverse().find(run => ["queued", "running", "waiting_user"].includes(run.status)) || null;
  const failedRun = [...runs].reverse().find(run => run.status === "failed") || null;
  const messages = Object.values(state.messages || {}).sort((a, b) => (a.sequence || 0) - (b.sequence || 0));

  const applyRunEvent = React.useCallback((runId, event) => {
    setState(previous => ChatState.applyEvent(previous, runId, event));
    const completed = event.payload?.kind === "run.status" && event.payload.status === "succeeded";
    if (completed) {
      getRun(runId).then(run => {
        if (run?.result_itinerary_id) onPlanResult?.(run.result_itinerary_id);
      }).catch(() => {});
    }
  }, [onPlanResult]);

  const subscribeRun = React.useCallback(run => {
    if (!run?.id || abortsRef.current[run.id]) return;
    let activeAbort = null;
    streamRuntimeRun(run.id, 0, {
      onAbort: abort => { activeAbort = abort; abortsRef.current[run.id] = abort; },
      onEvent: event => applyRunEvent(run.id, event),
      onClose: () => { if (abortsRef.current[run.id] === activeAbort) delete abortsRef.current[run.id]; },
      onError: () => { if (abortsRef.current[run.id] === activeAbort) delete abortsRef.current[run.id]; },
    });
  }, [applyRunEvent]);

  const loadConversation = React.useCallback(async conversationId => {
    setActiveId(conversationId);
    const [loadedMessages, loadedRuns] = await Promise.all([
      getConversationMessages(conversationId), listRuns(conversationId),
    ]);
    let next = ChatState.initialState();
    loadedMessages.forEach(message => { next = ChatState.upsertMessage(next, message); });
    loadedRuns.forEach(run => { next.runs[run.id] = run; });
    const activeRuns = loadedRuns.filter(run => ["queued", "running", "waiting_user"].includes(run.status));
    const histories = await Promise.all(activeRuns.map(run => getRunEvents(run.id, 0).catch(() => [])));
    activeRuns.forEach((run, index) => histories[index].forEach(event => { next = ChatState.applyEvent(next, run.id, event); }));
    setState(next);
    activeRuns.forEach(subscribeRun);
  }, [subscribeRun]);

  React.useEffect(() => {
    Object.values(abortsRef.current).forEach(abort => abort());
    abortsRef.current = {};
    setState(ChatState.initialState()); setActiveId(null); setError(""); setLoading(true);
    if (!currentUsername || !planId) { setLoading(false); return undefined; }
    let alive = true;
    listConversations(planId).then(async conversations => {
      if (!alive) return;
      const conversation = conversations.find(item => item.status !== "archived") || conversations[0];
      if (conversation) await loadConversation(conversation.id);
    }).catch(errorValue => { if (alive) setError(errorValue.message || "加载行程对话失败"); })
      .finally(() => { if (alive) setLoading(false); });
    return () => {
      alive = false;
      Object.values(abortsRef.current).forEach(abort => abort());
      abortsRef.current = {};
    };
  }, [planId, currentUsername]); // eslint-disable-line react-hooks/exhaustive-deps

  React.useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages.length, runs.map(run => `${run.id}:${run.status}`).join("|")]);

  const ensureConversation = async content => {
    if (activeId) return activeId;
    const created = await createConversation(`${plan.destination || "旅行"} · 行程调整`);
    setActiveId(created.id);
    return created.id;
  };

  const send = async () => {
    const text = query.trim();
    if (!text || (blockingRun && blockingRun.status !== "waiting_user")) return;
    setQuery(""); setError("");
    try {
      if (blockingRun?.status === "waiting_user") {
        const interactionId = blockingRun.pending_interaction?.interaction_id || blockingRun.outstanding_interaction_id;
        if (!interactionId) throw new Error("当前任务还不能接收回复");
        const resumed = await resumeRuntimeRun(blockingRun.id, interactionId, text);
        setState(previous => {
          let next = previous;
          if (resumed.accepted_message) next = ChatState.upsertMessage(next, resumed.accepted_message);
          return { ...next, runs: { ...next.runs, [blockingRun.id]: { ...next.runs[blockingRun.id], ...resumed } } };
        });
        abortsRef.current[blockingRun.id]?.();
        delete abortsRef.current[blockingRun.id];
        subscribeRun({ ...blockingRun, ...resumed });
        return;
      }
      const conversationId = await ensureConversation(text);
      const result = await submitConversationMessage(conversationId, text, { related_itinerary_id: planId });
      setState(previous => {
        let next = ChatState.upsertMessage(previous, result.message);
        next = { ...next, runs: { ...next.runs, [result.run.id]: result.run } };
        return next;
      });
      subscribeRun(result.run);
    } catch (errorValue) {
      setQuery(text);
      setError(errorValue.message || "发送失败，请重试");
    }
  };

  const controlRun = async (run, action) => {
    try {
      const updated = action === "cancel" ? await cancelRuntimeRun(run.id) : await retryRuntimeRun(run.id);
      setState(previous => ({ ...previous, runs: { ...previous.runs, [updated.id]: updated } }));
      if (action === "retry") subscribeRun(updated);
    } catch (errorValue) { setError(errorValue.message || "操作失败"); }
  };

  const dayLabel = `Day ${dayIdx + 1}`;
  const attractionCount = (day?.items || []).filter(item => item.type === "attraction").length;
  const mealCount = (day?.items || []).filter(item => item.type !== "attraction").length;
  const quickPrompts = [
    `${dayLabel} 行程轻松一点`,
    `优化 ${dayLabel} 的路线`,
    day?.theme ? `围绕“${day.theme}”换一个景点` : "换一个更合适的景点",
  ];
  return <aside className="detail-chat-canvas" aria-label="AI 行程对话">
    <DetailLightRays />
    <header className="detail-chat-header">
      <button className="detail-chat-back" onClick={onBack} aria-label="返回旅行工作区">‹</button>
      <div className="detail-chat-title"><strong>{plan.title || `${plan.destination || "目的地"}旅行`}</strong><small><i />规划已同步到地图 · {plan.days?.length || 1} 天</small></div>
      <div className="detail-chat-actions">
        <div className="detail-history-anchor"><button onClick={onHistory} aria-expanded={historyOpen}>历史修改</button>
          {historyOpen && <div className="detail-history-popover">
            <b>关联对话</b>
            {historyLoading ? <span>正在加载…</span> : historyItems.length ? historyItems.map(item => <span key={item.id}><strong>{item.title || "未命名对话"}</strong><small>{new Date(item.updated_at).toLocaleDateString()}</small></span>) : <span>还没有修改记录</span>}
          </div>}
        </div>
        <button onClick={onShare}>分享</button>
        <span className="detail-ai-badge"><i>✦</i>旅行助手</span>
      </div>
    </header>
    <div className="detail-chat-context" aria-label="当前行程上下文">
      <span><UiIcon name="location" size={13} />{plan.destination || "目的地"}</span>
      <span>{plan.date_range || "日期待定"}</span>
      <span>{plan.travelers || plan.people || "同行信息"}</span>
      <em>地图实时联动</em>
    </div>
    <main className="detail-chat-scroll" ref={scrollRef} role="log" aria-label="行程调整对话">
      <div className="detail-chat-time">当前行程 · {plan.date_range || "日期待定"}</div>
      <div className="detail-chat-message assistant"><span className="detail-chat-avatar"><Mascot size={25} pose="idle" /></span><div><small>旅行助手</small><p><b>{plan.destination || "这份行程"}</b> 已经整理好了。你可以直接告诉我想调整哪一天、哪个景点，或者希望整体节奏更松弛一些。<span className="detail-ai-note"><b><i />我正在结合右侧地图判断移动距离</b><span>修改会同时更新路线顺序、移动距离和右侧逐日安排。</span></span></p><em>已读取当前路线</em></div></div>
      {messages.map(message => <div className={`detail-chat-message ${message.role === "user" ? "user" : "assistant"}`} key={message.id}>
        {message.role !== "user" && <span className="detail-chat-avatar"><Mascot size={25} pose="idle" /></span>}
        <div>{message.role !== "user" && <small>旅行助手</small>}<p>{message.content}</p><em>{message.role === "user" ? "刚刚" : "已结合地图上下文"}</em></div>
      </div>)}
      <div className="detail-chat-message assistant detail-route-summary"><span className="detail-chat-avatar"><UiIcon name="map" size={14} /></span><div><small>{dayLabel} · 当前安排</small><p><b>{day?.theme || "今天的旅行安排"}</b></p><section><span>{attractionCount} 个景点</span><span>{mealCount} 个餐饮</span><span>地图已同步</span></section></div></div>
      {blockingRun && <div className="detail-run-card"><header><span className="detail-run-spinner" /><b>{blockingRun.status === "waiting_user" ? "需要你的回复" : "正在调整当前行程"}</b></header><p>{ChatState.runStatusText(blockingRun)}</p>{blockingRun.status !== "waiting_user" && <button onClick={() => controlRun(blockingRun, "cancel")}>停止</button>}</div>}
      {!blockingRun && failedRun && <div className="detail-run-card failed"><header><UiIcon name="alert" size={15} /><b>这次修改没有完成</b></header><p>{failedRun.error_public?.message || "可以保留输入并重新尝试。"}</p><button onClick={() => controlRun(failedRun, "retry")}>重新尝试</button></div>}
      {loading && <div className="detail-chat-loading">正在恢复关联对话…</div>}
      {error && <div className="detail-chat-error" role="alert">{error}</div>}
    </main>
    <div className="detail-composer-wrap">
      <div className="detail-quick-row">{quickPrompts.map(prompt => <button key={prompt} onClick={() => setQuery(prompt)}>{prompt}</button>)}</div>
      {editing && <p className="detail-edit-notice"><UiIcon name="edit" size={13} />保存后再提交对话修改。地点增删和换日会同步更新对应的必去与指定日要求；其余要求保留。</p>}
      <div className="detail-composer">
        <textarea value={query} onChange={event => setQuery(event.target.value)}
          disabled={editing || (!!blockingRun && blockingRun.status !== "waiting_user")}
          onKeyDown={event => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); send(); } }}
          placeholder={blockingRun?.status === "waiting_user" ? "回答上方问题，继续这次修改…" : blockingRun ? "行程调整中…" : "继续修改行程，例如：Day 3 不要安排太满…"} />
        <div className="detail-composer-footer"><button aria-label="添加"><UiIcon name="plus" size={15} /></button><button aria-label="引用地图"><UiIcon name="location" size={15} /></button><span>规划模式</span><em>Enter 发送 · Shift+Enter 换行</em>
          <button className="send" onClick={blockingRun && blockingRun.status !== "waiting_user" ? () => controlRun(blockingRun, "cancel") : send}
            disabled={editing || (blockingRun?.status === "waiting_user" ? !query.trim() : !blockingRun && !query.trim())}
            aria-label={blockingRun && blockingRun.status !== "waiting_user" ? "停止当前任务" : "发送消息"}><UiIcon name={blockingRun && blockingRun.status !== "waiting_user" ? "stop" : "arrow-up"} size={16} /></button>
        </div>
      </div>
    </div>
    {shareNotice && <div className="detail-share-notice" role="status">{shareNotice}</div>}
  </aside>;
}

/* ── 历史行程页 ───────────────────────────────── */
function HistoryPage({ onOpenPlan, currentUsername }) {
  const [trips, setTrips] = React.useState(null);
  const [loading, setLoading] = React.useState(true);

  React.useEffect(() => {
    getHistory().then(data => {
      setTrips(Array.isArray(data) ? data : []);
      setLoading(false);
    }).catch(() => { setTrips([]); setLoading(false); });
  }, []);

  const open = async (trip) => {
    try {
      const data = await getHistoryItem(trip.id);
      if (data && data.plan) onOpenPlan && onOpenPlan(data.plan, trip.id);
    } catch {}
  };

  return (
    <div className="page page-fade">
      <div className="mag-head">
        <div>
          <div className="eyebrow">ARCHIVE · 过往旅程</div>
          <h1>历史行程</h1>
        </div>
        {!loading && trips && (
          <div className="head-note">共 {trips.length} 期 · 点击封面回看<br />每一趟都是一期独立的「刊物」</div>
        )}
      </div>

      {loading && (
        <div className="skeleton-grid">
          {[1,2,3,4].map(i => <div key={i} className="skeleton-card" />)}
        </div>
      )}

      {!loading && trips && trips.length === 0 && (
        <div className="empty-state">
          <Mascot size={100} pose="think" />
          <div className="es-title">还没有行程记录</div>
          <div>先去新建一趟旅行吧！</div>
        </div>
      )}

      {!loading && trips && trips.length > 0 && (
        <div className="trip-grid">
          {trips.map((t, idx) => {
            const dest = t.destination || "旅行";
            const encDest = encodeURIComponent(dest);
            const imgUrl = `https://picsum.photos/seed/${encDest}-${t.id}/600/760`;
            const dates = (() => {
              const s = t.start_date ? t.start_date.replace(/-/g, ".").slice(2) : "";
              const e = t.end_date ? t.end_date.replace(/-/g, ".").slice(2) : "";
              return s && e ? `${s} — ${e}` : (t.created_at || "").slice(0, 10);
            })();
            const daysCount = t.days_count ||
              (t.start_date && t.end_date
                ? Math.ceil((new Date(t.end_date) - new Date(t.start_date)) / 86400000) + 1
                : 1);
            const isModified = !!t.parent_id;

            return (
              <div key={t.id} className="trip-cover"
                onClick={() => open(t)} role="button" tabIndex={0}
                onKeyDown={(e) => e.key === "Enter" && open(t)}>
                <div className="tc-img" style={{ backgroundImage: `url('${imgUrl}')` }}></div>
                <div className="tc-shade"></div>
                <div className="tc-top">
                  <span>VOL.{String(idx + 1).padStart(2, "0")}</span>
                  <span>{daysCount} DAYS</span>
                </div>
                <div className="tc-body">
                  <div className="tc-dest">{dest}</div>
                  <div className="tc-dates">{dates}</div>
                  <div className="tc-badges">
                    <span className="tc-badge">{daysCount} 天行程</span>
                    {isModified && <span className="tc-badge modified">修改版</span>}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

/* ── 画像页 ──────────────────────────────────── */
function TagEditor({ label, hint, tags, onChange }) {
  const [val, setVal] = React.useState("");

  const add = () => {
    const v = val.trim().replace(/[,，]$/, "");
    if (v && !tags.includes(v)) onChange([...tags, v]);
    setVal("");
  };

  const remove = (t) => onChange(tags.filter(x => x !== t));

  return (
    <div className="pf-field">
      <div className="pf-label">{label}<span className="pf-hint">{hint}</span></div>
      <div className="tag-editor">
        {tags.map((t) => (
          <span key={t} className="tag">{t}
            <button onClick={() => remove(t)} aria-label={`删除 ${t}`}><UiIcon name="close" size={12} /></button>
          </span>
        ))}
        <input className="tag-input" value={val} placeholder="输入后回车添加…"
          onChange={(e) => setVal(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" || e.key === "," || e.key === "，") { e.preventDefault(); add(); } }}
          onBlur={() => val.trim() && add()} />
      </div>
    </div>
  );
}

const MEMORY_CATEGORY_LABELS = {
  attraction_preference: "旅行主题", food_preference: "餐饮偏好", dietary_requirement: "饮食要求",
  travel_pace: "旅行节奏", budget_style: "预算习惯", transport_preference: "交通偏好",
  accommodation_preference: "住宿偏好", schedule_preference: "时间习惯", companion_context: "同行情境",
  accessibility_need: "无障碍需求", destination_history: "去过的地方", other_travel_preference: "其他偏好",
};
const MEMORY_POLARITY_LABELS = { prefer: "喜欢", avoid: "避开", require: "需要", fact: "事实" };
const MEMORY_SCOPE_LABELS = { global: "所有旅行", destination: "特定目的地", companion: "特定同行人", destination_companion: "目的地与同行人" };

function MemoryFactCard({ fact, candidate = false, onChanged }) {
  const [editing, setEditing] = React.useState(false);
  const [value, setValue] = React.useState(fact.value_text);
  const [category, setCategory] = React.useState(fact.category);
  const [polarity, setPolarity] = React.useState(fact.polarity);
  const [scopeType, setScopeType] = React.useState(fact.scope_type);
  const [destination, setDestination] = React.useState(fact.scope_key?.destination || "");
  const [companion, setCompanion] = React.useState(fact.scope_key?.companion || "");
  const [busy, setBusy] = React.useState(false);
  const scopeDetail = Object.values(fact.scope_key || {}).filter(Boolean).join(" · ");
  const run = async action => {
    setBusy(true);
    try { await action(); await onChanged(); setEditing(false); }
    catch (e) { alert(e.message || "操作失败"); }
    finally { setBusy(false); }
  };
  return (
    <article className={`memory-fact-card ${candidate ? "candidate" : "active"}`}>
      <div className="memory-fact-mark" aria-hidden="true">{candidate ? "?" : <UiIcon name="check" size={14} />}</div>
      <div className="memory-fact-body">
        <div className="memory-fact-meta">
          <span>{MEMORY_CATEGORY_LABELS[fact.category] || fact.category}</span>
          <em>{MEMORY_POLARITY_LABELS[fact.polarity] || fact.polarity}</em>
          <small>{MEMORY_SCOPE_LABELS[fact.scope_type] || fact.scope_type}{scopeDetail ? ` · ${scopeDetail}` : ""}</small>
        </div>
        {editing ? (
          <div className="memory-inline-edit">
            <select value={category} onChange={e => setCategory(e.target.value)}>{Object.entries(MEMORY_CATEGORY_LABELS).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select>
            <select value={polarity} onChange={e => setPolarity(e.target.value)}>{Object.entries(MEMORY_POLARITY_LABELS).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select>
            <select value={scopeType} onChange={e => setScopeType(e.target.value)}>{Object.entries(MEMORY_SCOPE_LABELS).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select>
            {(scopeType === "destination" || scopeType === "destination_companion") && <input value={destination} onChange={e => setDestination(e.target.value)} placeholder="目的地，例如：日本" />}
            {(scopeType === "companion" || scopeType === "destination_companion") && <input value={companion} onChange={e => setCompanion(e.target.value)} placeholder="同行情境，例如：带孩子" />}
            <input value={value} onChange={e => setValue(e.target.value)} autoFocus />
            <button disabled={busy || !value.trim() || ((scopeType === "destination" || scopeType === "destination_companion") && !destination.trim()) || ((scopeType === "companion" || scopeType === "destination_companion") && !companion.trim())} onClick={() => run(() => updateMemoryFact(fact.id, {
              category, value_text: value.trim(), polarity, scope_type: scopeType,
              scope_key: scopeType === "global" ? {} : {
                ...(scopeType === "destination" || scopeType === "destination_companion" ? { destination: destination.trim() } : {}),
                ...(scopeType === "companion" || scopeType === "destination_companion" ? { companion: companion.trim() } : {}),
              },
            }))}>保存</button>
            <button disabled={busy} onClick={() => { setValue(fact.value_text); setEditing(false); }}>取消</button>
          </div>
        ) : <strong>{fact.value_text}</strong>}
        <p>{candidate ? (fact.sensitivity === "protected" ? "这条信息较敏感，确认后才会用于新的旅行对话。" : "小way从对话中推测了这条习惯，请你确认。") : `来源：${fact.source_kind === "manual" ? "你手动添加" : fact.source_kind === "legacy" ? "旧画像迁移" : "旅行对话"}`}</p>
      </div>
      <div className="memory-fact-actions">
        {candidate && <button className="memory-approve" disabled={busy} onClick={() => run(() => approveMemoryFact(fact.id))}>确认记住</button>}
        {!editing && <button disabled={busy} onClick={() => setEditing(true)}>{candidate ? "编辑并确认" : "编辑"}</button>}
        <button className="memory-forget" disabled={busy} onClick={() => run(() => deleteMemoryFact(fact.id))}>{candidate ? "忽略" : "忘记"}</button>
      </div>
    </article>
  );
}

function ProfilePage({ currentUsername }) {
  const [profile, setProfile] = React.useState({ revision: 0, active_facts: [], candidate_facts: [], trip_count: 0 });
  const [loading, setLoading] = React.useState(true);
  const [form, setForm] = React.useState({ category: "attraction_preference", value_text: "", polarity: "prefer", scope_type: "global", destination: "", companion: "" });
  const [adding, setAdding] = React.useState(false);
  const refreshProfile = async () => {
    try {
      const data = await getProfile();
      if (data) setProfile(data);
    } catch {}
  };

  React.useEffect(() => {
    refreshProfile().finally(() => setLoading(false));
    const onVisible = () => { if (!document.hidden) refreshProfile(); };
    window.addEventListener("focus", onVisible);
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.removeEventListener("focus", onVisible);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, []);
  const addFact = async e => {
    e.preventDefault();
    if (!form.value_text.trim()) return;
    setAdding(true);
    try {
      const scope_key = form.scope_type === "global" ? {} : {
        ...(form.scope_type === "destination" || form.scope_type === "destination_companion" ? { destination: form.destination.trim() } : {}),
        ...(form.scope_type === "companion" || form.scope_type === "destination_companion" ? { companion: form.companion.trim() } : {}),
      };
      const { destination, companion, ...payload } = form;
      await createMemoryFact({ ...payload, value_text: form.value_text.trim(), scope_key });
      setForm(previous => ({ ...previous, value_text: "", destination: "", companion: "" }));
      await refreshProfile();
    } catch (e) { alert(e.message || "添加失败"); }
    finally { setAdding(false); }
  };

  const username = currentUsername || getAuth()?.username || "旅行者";
  const cityCount = profile.active_facts.filter(item => item.category === "destination_history").length;
  const groups = Object.entries(profile.active_facts.reduce((acc, fact) => {
    const key = MEMORY_CATEGORY_LABELS[fact.category] || "其他记忆";
    (acc[key] ||= []).push(fact); return acc;
  }, {}));

  return (
    <div className="page page-fade">
      <div className="mag-head">
        <div>
          <div className="eyebrow">PROFILE · 旅行画像</div>
          <h1>我的旅行画像</h1>
        </div>
        <div className="head-note">只把你确认过的旅行习惯<br />带进下一段对话</div>
      </div>

      <div className="profile-grid">
        <aside className="profile-aside">
          <div style={{ display: "grid", placeItems: "center" }}>
            <Mascot size={120} pose="idle" />
          </div>
          <div className="pa-name">{username}</div>
          <div className="pa-sub">记忆版本 {profile.revision} · 每段对话都会冻结一份独立快照</div>
          <div className="pa-stats">
            <div className="pa-stat"><div className="ps-n">{profile.trip_count}</div><div className="ps-l">趟旅程</div></div>
            <div className="pa-stat"><div className="ps-n">{cityCount}</div><div className="ps-l">座城市</div></div>
            <div className="pa-stat">
              <div className="ps-n">{profile.active_facts.length}</div><div className="ps-l">条记忆</div>
            </div>
          </div>
        </aside>

        <div>
          <form className="memory-add-card" onSubmit={addFact}>
            <div><span>ADD A MEMORY</span><strong>告诉小way一条稳定的旅行习惯</strong></div>
            <div className="memory-add-grid">
              <select value={form.category} onChange={e => setForm({ ...form, category: e.target.value })}>{Object.entries(MEMORY_CATEGORY_LABELS).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select>
              <select value={form.polarity} onChange={e => setForm({ ...form, polarity: e.target.value })}>{Object.entries(MEMORY_POLARITY_LABELS).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select>
              <select value={form.scope_type} onChange={e => setForm({ ...form, scope_type: e.target.value })}>{Object.entries(MEMORY_SCOPE_LABELS).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select>
              {(form.scope_type === "destination" || form.scope_type === "destination_companion") && <input value={form.destination} onChange={e => setForm({ ...form, destination: e.target.value })} placeholder="目的地，例如：日本" required />}
              {(form.scope_type === "companion" || form.scope_type === "destination_companion") && <input value={form.companion} onChange={e => setForm({ ...form, companion: e.target.value })} placeholder="同行情境，例如：带孩子" required />}
              <input className="memory-value-input" value={form.value_text} onChange={e => setForm({ ...form, value_text: e.target.value })} placeholder="例如：旅行时通常喜欢慢节奏，每天不超过三个景点" />
              <button disabled={adding || !form.value_text.trim()}>{adding ? "正在记住…" : "记住这条"}</button>
            </div>
          </form>
          {loading ? <div className="memory-loading">正在翻开你的旅行档案…</div> : <>
            {profile.candidate_facts.length > 0 && <section className="memory-section candidate-section"><header><span>NEEDS YOUR WORD</span><h2>待你确认</h2><p>推断或较敏感的信息不会自动用于新对话。</p></header><div className="memory-fact-list">{profile.candidate_facts.map(fact => <MemoryFactCard key={fact.id} fact={fact} candidate onChanged={refreshProfile} />)}</div></section>}
            <section className="memory-section"><header><span>TRAVEL MEMORY</span><h2>小way已经记住</h2><p>当前对话不会中途刷新；这些变化会从下一段新对话开始生效。</p></header>
              {groups.length ? groups.map(([label, facts]) => <div className="memory-group" key={label}><h3>{label}<small>{facts.length}</small></h3><div className="memory-fact-list">{facts.map(fact => <MemoryFactCard key={fact.id} fact={fact} onChanged={refreshProfile} />)}</div></div>) : <div className="memory-empty">还没有长期旅行记忆。你可以先添加一条，或在聊完后归档对话。</div>}
            </section>
          </>}
        </div>
      </div>
    </div>
  );
}

/* ── Sweep 预览页 ─────────────────────────────────── */
/* ── 首页 / 计划中心 ─────────────────────────────── */
function DashboardPlanCard({ trip, onOpen }) {
  const [revealed, setRevealed] = React.useState(false);
  const cardRef = React.useRef(null);

  React.useEffect(() => {
    if (typeof IntersectionObserver === "undefined") { setRevealed(true); return undefined; }
    const card = cardRef.current;
    if (!card) return undefined;
    const observer = new IntersectionObserver(entries => {
      if (entries.some(entry => entry.isIntersecting)) {
        setRevealed(true);
        observer.disconnect();
      }
    }, { threshold: .16, rootMargin: "0px 0px -7%" });
    observer.observe(card);
    return () => observer.disconnect();
  }, []);

  const destination = trip.destination || "旅行";
  const days = trip.start_date && trip.end_date ? Math.ceil((new Date(trip.end_date) - new Date(trip.start_date)) / 86400000) + 1 : 1;
  const date = trip.start_date && trip.end_date ? `${trip.start_date.slice(5).replace("-", ".")} — ${trip.end_date.slice(5).replace("-", ".")}` : "日期待定";
  return <button ref={cardRef} className={`dashboard-plan-card ${revealed ? "is-revealed" : ""}`} onFocus={() => setRevealed(true)} onClick={onOpen}>
    <ItineraryCover className="dashboard-plan-image" planId={trip.id} placeholder="暂无封面" />
    <span className="dashboard-plan-info"><i>{trip.parent_id ? `修改版 V${trip.version}` : "旅行计划"}</i><strong>{destination}{` · ${days}日游`}</strong><small>{date} · {days} 天</small><em>查看行程 <UiIcon name="arrow-right" size={14} /></em></span>
  </button>;
}

const SCENIC_SLIDES = [
  { destination: "南京", src: "/assets/home-scenery/nanjing.jpg", alt: "南京玄武湖与紫金山", title: "南京 · 玄武湖与紫金山", author: "Farm", source: "https://commons.wikimedia.org/wiki/File:Nanjing_XuanWuLake_Purple_and_Mountain.jpg", license: "CC BY-SA 2.5", licenseUrl: "https://creativecommons.org/licenses/by-sa/2.5/" },
  { destination: "苏州", src: "/assets/home-scenery/suzhou.jpg", alt: "苏州拙政园的花木与园林", title: "苏州 · 拙政园", author: "Rose Abrams", source: "https://commons.wikimedia.org/wiki/File:Touring_the_Humble_Administrator%27s_Garden_01.jpg", license: "CC BY 4.0", licenseUrl: "https://creativecommons.org/licenses/by/4.0/" },
];

function ScenicCarousel({ destination = "", onStart }) {
  const slides = destination ? SCENIC_SLIDES.filter(slide => slide.destination === destination) : SCENIC_SLIDES;
  const [index, setIndex] = React.useState(0);
  const [paused, setPaused] = React.useState(false);
  const [hovered, setHovered] = React.useState(false);
  const [focused, setFocused] = React.useState(false);
  const [failedSrc, setFailedSrc] = React.useState("");
  const [reduced, setReduced] = React.useState(() => window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  React.useEffect(() => {
    const media = window.matchMedia("(prefers-reduced-motion: reduce)");
    const update = event => setReduced(event.matches);
    media.addEventListener?.("change", update);
    return () => media.removeEventListener?.("change", update);
  }, []);
  React.useEffect(() => setIndex(0), [destination]);
  React.useEffect(() => {
    if (paused || reduced || hovered || focused || slides.length < 2) return undefined;
    const timer = window.setInterval(() => {
      if (!document.hidden) setIndex(value => (value + 1) % slides.length);
    }, 6000);
    return () => window.clearInterval(timer);
  }, [paused, reduced, hovered, focused, slides.length]);
  const slide = slides[index % slides.length];
  const changeSlide = direction => setIndex(value => (value + direction + slides.length) % slides.length);
  return <section className="scenic-carousel" aria-label="风景灵感轮播" aria-roledescription="轮播"
    onMouseEnter={() => setHovered(true)} onMouseLeave={() => setHovered(false)}
    onFocusCapture={() => setFocused(true)}
    onBlurCapture={event => { if (!event.currentTarget.contains(event.relatedTarget)) setFocused(false); }}>
    {slide && failedSrc !== slide.src
      ? <img key={slide.src} src={slide.src} alt={slide.alt} onError={() => setFailedSrc(slide.src)} />
      : <div className="scenic-placeholder">
          <UiIcon name="camera" size={32} />
          <strong>{destination ? `${destination}风景图片暂不可用` : "风景图片暂不可用"}</strong>
          <span>你仍然可以继续填写旅行想法</span>
          {slide && <button onClick={() => setFailedSrc("")}>重试图片</button>}
        </div>}
    <div className="scenic-caption">
      <small>目的地灵感 · 参考照片</small>
      <h2>{slide?.title || (destination ? `下一站，${destination}` : "从一处风景开始")}</h2>
      {slide && <div className="scenic-controls">
        <button aria-label="上一张风景" disabled={slides.length < 2} onClick={() => changeSlide(-1)}><span className="scenic-back-arrow"><UiIcon name="arrow-right" size={16} /></span></button>
        <span>{index % slides.length + 1} / {slides.length}</span>
        <button aria-label="下一张风景" disabled={slides.length < 2} onClick={() => changeSlide(1)}><UiIcon name="arrow-right" size={16} /></button>
        <button aria-label={reduced ? "减少动画已启用" : paused ? "播放风景轮播" : "暂停风景轮播"}
          disabled={reduced || slides.length < 2} onClick={() => setPaused(value => !value)}>{reduced ? "静止展示" : paused ? "播放" : "暂停"}</button>
        <button className="scenic-start" onClick={() => onStart?.(`${slide.destination}，`)}>去{slide.destination}<UiIcon name="arrow-right" size={14} /></button>
      </div>}
      {slide && <div className="scenic-credit"><a href={slide.source} target="_blank" rel="noreferrer">{slide.author}</a> · <a href={slide.licenseUrl} target="_blank" rel="noreferrer">{slide.license}</a></div>}
    </div>
  </section>;
}

function DashboardPage({ currentUsername, onStart, onDraftChange, onOpenPlan, onOpenWorkspace, planOpenError = "" }) {
  const [draft, setDraft] = React.useState("");

  const [planFeed, setPlanFeed] = React.useState(() => ({
    owner: currentUsername, items: [], cursor: null,
    status: currentUsername ? "loading" : "ready", paging: false, error: "",
  }));
  const historyRequestRef = React.useRef({ id: 0, busy: false });
  const usernameRef = React.useRef(currentUsername);
  usernameRef.current = currentUsername;
  const plansSentinelRef = React.useRef(null);
  const cities = ["北京", "上海", "南京", "苏州", "广州", "杭州", "成都", "重庆"];
  // Mask old account data during the render before the identity effect runs.
  const feed = planFeed.owner === currentUsername ? planFeed : {
    items: [], cursor: null, status: currentUsername ? "loading" : "ready", paging: false, error: "",
  };
  const plans = feed.items;
  const plansCursor = feed.cursor;
  const loadingPlans = feed.status === "loading";
  const loadingMorePlans = feed.paging;
  const plansError = feed.error;

  const loadPlans = React.useCallback(async (cursor = null) => {
    const owner = currentUsername;
    if (!owner || usernameRef.current !== owner || (cursor && historyRequestRef.current.busy)) return;
    const requestId = ++historyRequestRef.current.id;
    historyRequestRef.current.busy = true;
    setPlanFeed(previous => cursor
      ? { ...previous, paging: true, error: "" }
      : { owner, items: [], cursor: null, status: "loading", paging: false, error: "" });
    const isCurrent = () => usernameRef.current === owner && historyRequestRef.current.id === requestId;
    try {
      const page = await getHistoryPage(6, cursor);
      if (!isCurrent()) return;
      setPlanFeed(previous => ({
        owner, items: cursor
          ? [...previous.items, ...page.items.filter(item => !previous.items.some(existing => existing.id === item.id))]
          : page.items,
        cursor: page.next_cursor, status: "ready", paging: false, error: "",
      }));
    } catch (error) {
      if (!isCurrent()) return;
      setPlanFeed(previous => ({ ...previous, status: cursor ? "ready" : "error", paging: false,
        error: error.message || "历史行程暂时无法加载" }));
    } finally {
      if (isCurrent()) historyRequestRef.current.busy = false;
    }
  }, [currentUsername]);

  React.useEffect(() => {
    historyRequestRef.current.id++;
    historyRequestRef.current.busy = false;
    setPlanFeed({ owner: currentUsername, items: [], cursor: null,
      status: currentUsername ? "loading" : "ready", paging: false, error: "" });
    if (currentUsername) loadPlans();
    return () => { historyRequestRef.current.id++; historyRequestRef.current.busy = false; };
  }, [currentUsername, loadPlans]);

  React.useEffect(() => {
    if (!plansCursor || loadingPlans || loadingMorePlans || plansError || typeof IntersectionObserver === "undefined") return undefined;
    const sentinel = plansSentinelRef.current;
    if (!sentinel) return undefined;
    const observer = new IntersectionObserver(entries => {
      if (entries.some(entry => entry.isIntersecting)) loadPlans(plansCursor);
    }, { rootMargin: "280px 0px" });
    observer.observe(sentinel);
    return () => observer.disconnect();
  }, [plansCursor, loadingPlans, loadingMorePlans, plansError, loadPlans, plans.length]);


  React.useEffect(() => {
    if (!currentUsername) return undefined;
    const refresh = () => { if (!document.hidden) loadPlans(); };
    window.addEventListener("focus", refresh);
    document.addEventListener("visibilitychange", refresh);
    return () => {
      window.removeEventListener("focus", refresh);
      document.removeEventListener("visibilitychange", refresh);
    };
  }, [currentUsername, loadPlans]);

  const begin = (text = draft) => {
    const prompt = text.trim();
    onStart?.(prompt);
  };

  return <main className="dashboard-page page-fade">
    <section className="dashboard-hero scenery-hero" aria-labelledby="dashboard-title">
      <div className="dashboard-hero-main">
        <div className="dashboard-hero-copy">
          <div className="scenery-eyebrow">和小way一起出发</div>
          <h1 id="dashboard-title">先看风景，<br />再决定如何出发。</h1>
          <p>从一个想去的地方开始。<br />把日期、人数和偏好交给小way，一起慢慢整理旅行计划。</p>
        </div>
        <div className="dashboard-composer">
          <textarea value={draft} onChange={event => { setDraft(event.target.value); onDraftChange?.(event.target.value); }}
            aria-label="描述旅行想法" placeholder="想去哪里，几天，和谁一起？"
            onKeyDown={event => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); begin(); } }} />
          <div className="dashboard-composer-foot">
            <span>进入工作区后，可以继续补充需求</span>
            <button className="dashboard-send" onClick={() => begin()} aria-label="开始旅行规划">开始规划<UiIcon name="arrow-right" size={16} /></button>
          </div>
          <div className="dashboard-style-chips">
            {["南京", "苏州", "杭州"].map(city => <button key={city} aria-label={`填入${city}旅行草稿`} onClick={() => { setDraft(`${city}，`); onDraftChange?.(`${city}，`); }}>{city}</button>)}
          </div>
        </div>
      </div>
      <ScenicCarousel destination={cities.find(city => draft.includes(city)) || (draft.includes("韩国") ? "韩国" : "")} onStart={begin} />
    </section>

    <section className="dashboard-cities" aria-label="热门目的地">
      <strong>热门目的地</strong>
      <div>{cities.map(city => <button key={city} onClick={() => begin(`${city}，`)}>{city}</button>)}</div>
    </section>

    <section className="dashboard-plans" aria-labelledby="dashboard-plans-title">
      {(!currentUsername || (!loadingPlans && !plansError && !plans.length)) ? <>
        <div className="dashboard-section-head"><div><span>WEEKEND IDEAS</span><h2 id="dashboard-plans-title">下个周末去哪里</h2></div>{currentUsername && <button onClick={() => loadPlans()}>刷新我的计划</button>}</div>
        <div className="dashboard-weekend-grid">
          {[{city:"南京",title:"南京周末",description:"沿湖散步，再找一处街巷慢慢逛。"}, {city:"苏州",title:"苏州周末",description:"看看园林，给自己留一点发呆的时间。"}].map(idea =>
            <button key={idea.city} className="dashboard-weekend-card" onClick={() => begin(`${idea.city}，`)}>
              <span className="dashboard-weekend-city">{idea.city}</span>
              <strong>{idea.title}</strong><span>{idea.description}</span>
              <em>从这里开始 <UiIcon name="arrow-right" size={14} /></em>
            </button>)}
        </div>
      </> : <>
        <div className="dashboard-section-head"><div><span>MY PLANS</span><h2 id="dashboard-plans-title">我的计划</h2></div><div className="dashboard-plan-actions"><button disabled={loadingPlans} onClick={() => loadPlans()}>刷新我的计划</button><button onClick={onOpenWorkspace}>进入旅行工作区 <UiIcon name="arrow-right" size={15} /></button></div></div>
        {planOpenError && <div className="dashboard-plan-empty dashboard-plan-error" role="alert">{planOpenError}</div>}
        {loadingPlans && <div className="dashboard-plan-empty" role="status">正在加载我的计划…</div>}
        {!loadingPlans && !plans.length && !!plansError && <div className="dashboard-plan-empty dashboard-plan-error" role="alert">{plansError}<button onClick={() => loadPlans()}>重新加载</button></div>}
        {!!plans.length && <div className="dashboard-plan-grid">
          {plans.map(trip => <DashboardPlanCard key={trip.id} trip={trip} onOpen={() => onOpenPlan?.(trip.id)} />)}
        </div>}
        {!!plans.length && <div className="dashboard-plans-more" ref={plansSentinelRef} aria-live="polite">
          {loadingMorePlans && <span>正在展开更多计划…</span>}
          {!loadingMorePlans && !!plansError && <button onClick={() => loadPlans(plansCursor)}>加载失败，重新尝试</button>}
          {!loadingMorePlans && !plansError && plansCursor && <button onClick={() => loadPlans(plansCursor)}>加载更多计划</button>}
          {!loadingMorePlans && !plansError && !plansCursor && <span>已展示全部计划</span>}
        </div>}
      </>}
    </section>
  </main>;
}

/* ── 首页（产品介绍落地页，保留供展示） ─────────── */
function HomePage({ onStart }) {
  const videoRef = React.useRef(null);

  // 自动播放兜底：标签 autoplay 在部分浏览器被拦截时，手动 play；返回前台/首次交互再试
  React.useEffect(() => {
    const play = () => { videoRef.current?.play?.().catch(() => {}); };
    play();
    const onVisible = () => { if (!document.hidden) play(); };
    const onPointer = () => { play(); };
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("pointerdown", onPointer, { once: true });
    return () => {
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("pointerdown", onPointer);
    };
  }, []);

  const viewAbilities = () =>
    document.getElementById("home-abilities")?.scrollIntoView({ behavior: "smooth", block: "start" });

  return (
    <div className="home-page page-fade">
      <main className="hero-shell">
        <div className="video-poster" aria-hidden="true"></div>
        <video
          ref={videoRef}
          className="hero-video"
          autoPlay loop muted playsInline preload="auto"
          poster="https://images.unsplash.com/photo-1557683316-973673baf926?w=1600&q=60"
        >
          <source src="https://d8j0ntlcm91z4.cloudfront.net/user_38xzZboKViGWJOttwIXH07lWA1P/hf_20260424_064411_9e9d7f84-9277-41f4-ab10-59172d89e6be.mp4" type="video/mp4" />
        </video>

        <section className="home-hero" id="start">
          <div className="hero-copy">
            <div className="hero-eyebrow">AI Travel Planner · Product Intro</div>
            <h1>你只管<br />期待出发。</h1>
            <p className="hero-lede">
              从灵感到出发，WAygent帮你把复杂的旅行决策变成一份真正可执行的计划。<br />
              多位 AI Agent 协同完成景点检索、路线编排、餐饮建议、天气参考与交通校验，<br />
              在几分钟内生成清晰、好读、可落地的旅行方案。
            </p>
            <div className="hero-actions">
              <button className="go-btn" onClick={onStart}>立即开始规划 <span aria-hidden="true">→</span></button>
              <button className="ghost-btn" onClick={viewAbilities}>查看产品能力</button>
            </div>
          </div>

          <aside className="capability-panel" aria-label="规划能力">
            <div className="panel-title">Planning Capabilities</div>
            <div className="capability-list">
              <article className="capability-item">
                <span className="capability-icon" aria-hidden="true">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor">
                    <path d="M16 20v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" />
                    <circle cx="10" cy="7" r="4" />
                    <path d="M20 20v-2a4 4 0 0 0-3-3.87" />
                    <path d="M17 3.13a4 4 0 0 1 0 7.75" />
                  </svg>
                </span>
                <span>
                  <h2>多 Agent 协同规划</h2>
                  <p>专业分工，高效协作</p>
                </span>
              </article>
              <article className="capability-item">
                <span className="capability-icon" aria-hidden="true">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor">
                    <circle cx="12" cy="12" r="9" />
                    <path d="M12 7v5l3 2" />
                  </svg>
                </span>
                <span>
                  <h2>自动校验路线与时间</h2>
                  <p>时间、交通、天气智能校验</p>
                </span>
              </article>
              <article className="capability-item">
                <span className="capability-icon" aria-hidden="true">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor">
                    <path d="M6 3h9l4 4v14H6z" />
                    <path d="M14 3v5h5" />
                    <path d="M9 13h6" />
                    <path d="M9 17h6" />
                  </svg>
                </span>
                <span>
                  <h2>输出可执行行程</h2>
                  <p>清晰、好读、可直接使用</p>
                </span>
              </article>
            </div>
          </aside>
        </section>

        <section className="features" id="home-abilities" aria-label="产品能力">
          <div className="feature-grid">
            <article className="feature-card">
              <div className="feature-no">01</div>
              <div className="feature-row">
                <span className="feature-icon" aria-hidden="true">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor">
                    <circle cx="11" cy="11" r="7" />
                    <path d="m20 20-3.4-3.4" />
                  </svg>
                </span>
                <span>
                  <h2>需求理解</h2>
                  <p>识别目的地、天数、用户偏好</p>
                </span>
              </div>
              <span className="card-arrow" aria-hidden="true">→</span>
            </article>
            <article className="feature-card">
              <div className="feature-no">02</div>
              <div className="feature-row">
                <span className="feature-icon" aria-hidden="true">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor">
                    <path d="M6 18c3-7 9-5 12-12" />
                    <path d="M7 7h.01" />
                    <path d="M17 17h.01" />
                    <path d="M8 7a2 2 0 1 1-4 0 2 2 0 0 1 4 0Z" />
                    <path d="M20 17a2 2 0 1 1-4 0 2 2 0 0 1 4 0Z" />
                  </svg>
                </span>
                <span>
                  <h2>智能规划</h2>
                  <p>拆解景点、交通、餐饮与节奏安排</p>
                </span>
              </div>
              <span className="card-arrow" aria-hidden="true">→</span>
            </article>
            <article className="feature-card">
              <div className="feature-no">03</div>
              <div className="feature-row">
                <span className="feature-icon" aria-hidden="true">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor">
                    <path d="M8 2v4" />
                    <path d="M16 2v4" />
                    <rect x="4" y="5" width="16" height="16" rx="2" />
                    <path d="M8 13l3 3 5-6" />
                  </svg>
                </span>
                <span>
                  <h2>路线校验</h2>
                  <p>结合时间、天气与通行方式优化行程</p>
                </span>
              </div>
              <span className="card-arrow" aria-hidden="true">→</span>
            </article>
            <article className="feature-card">
              <div className="feature-no">04</div>
              <div className="feature-row">
                <span className="feature-icon" aria-hidden="true">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor">
                    <path d="M6 3h9l4 4v14H6z" />
                    <path d="M14 3v5h5" />
                    <path d="M9 13h6" />
                    <path d="M9 17h4" />
                  </svg>
                </span>
                <span>
                  <h2>结果交付</h2>
                  <p>生成清晰、可执行、可编辑的旅游计划</p>
                </span>
              </div>
              <span className="card-arrow" aria-hidden="true">→</span>
            </article>
          </div>
        </section>
      </main>
    </div>
  );
}

Object.assign(window, { ChatPage, LegacyTripDetailPage, HistoryPage, ProfilePage, AuthModal });

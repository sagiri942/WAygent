// detail-redesign.jsx — 行程详情 redesign（真实行程 / 高德地图 / AI 会话）

function TransportLegDetails({ leg }) {
  if (!leg) return null;
  const names = { walking: "步行", bus: "公交", subway: "地铁", transit: "公交/地铁换乘", taxi: "打车" };
  return <details className="rd-transport" aria-label="景点间交通方案">
    <summary>{leg.origin} → {leg.destination}：{names[leg.mode] || leg.mode}，预计 {leg.minutes} 分钟{leg.cost_cents != null ? `，约 ¥${(leg.cost_cents / 100).toFixed(2)}` : "，费用未获取"}</summary>
    <p>已获取且符合限制的方案中选择；全程有预算时统筹分配。仅包含景点之间的交通。</p>
    {leg.buffer_min > 0 && <p>含叫车等待及上下车缓冲 {leg.buffer_min} 分钟（可在规划需求中调整）。</p>}
    <ul>{(leg.segments || []).map((part, index) => <li key={index}>{names[part.mode] || part.mode} {part.name || ""} · {part.minutes} 分钟{part.departure_stop ? ` · ${part.departure_stop} → ${part.arrival_stop || "目的站"}` : ""}</li>)}</ul>
    <small>查询时间：{leg.queried_at || leg.query_date || "未记录"}；采用规划当天的预计信息，实际出行可能变化。</small>
  </details>;
}

function RedesignTimeline({ items, selectedIndex, onSelect, onNav, activeNavKey, tipStatus }) {
  const selectedRef = React.useRef(null);
  const lastSelectionRef = React.useRef(items[selectedIndex]?.selectionId);
  React.useEffect(() => {
    const id = items[selectedIndex]?.selectionId;
    if (lastSelectionRef.current !== id) selectedRef.current?.scrollIntoView?.({ block: "nearest", inline: "nearest" });
    lastSelectionRef.current = id;
  }, [items[selectedIndex]?.selectionId]);
  return <div className="rd-timeline">
    <i className="rd-timeline-line" aria-hidden="true" />
    {(items || []).map((item, index) => {
      const next = items[index + 1];
      const navKey = next ? `redesign:${index}` : null;
      const isMeal = item.type !== "attraction";
      const note = item.note || item.reason || item.address || item.addr
        || (["queued", "running"].includes(tipStatus)
          ? "小way正在整理这处地点的旅行贴士…"
          : "已同步到当前路线，点开地图标记可查看详情。");
      const duration = itemDuration(item);
      return <React.Fragment key={item.selectionId || index}>
        {item.type === "attraction" && <TransportLegDetails leg={item.transport} />}
        <article ref={selectedIndex === index ? selectedRef : null} className={`rd-stop${selectedIndex === index ? " is-selected" : ""}`} role="button" tabIndex={0} aria-label={`选择安排：${item.name || "待安排地点"}`} aria-pressed={selectedIndex === index} onClick={() => onSelect(index)} onKeyDown={event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onSelect(index); } }}>
          <time><strong>{item.start || (item.type === "lunch" ? "午餐" : item.type === "dinner" ? "晚餐" : "待定")}</strong><span>{index === 0 ? "出发" : "之后"}</span></time>
          <span className={`rd-stop-node${isMeal ? " is-meal" : ""}`}><UiIcon name={isMeal ? "utensils" : "location"} size={14} /></span>
          <div className="rd-stop-card">
            <span className={`rd-stop-icon ${isMeal ? "ochre" : index % 3 === 0 ? "terracotta" : index % 3 === 1 ? "sage" : "ink"}`}><UiIcon name={isMeal ? "utensils" : index % 2 ? "map" : "location"} size={17} /></span>
            <div className="rd-stop-copy">
              <div className="rd-stop-meta"><span>{isMeal ? "本地风味" : "人文漫游"}</span><small>{duration === "建议停留" ? "停留时间待定" : `建议停留 ${duration}`}</small></div>
              <h4>{item.name || "待安排地点"}</h4><p>{note}</p>
              <em><UiIcon name="location" size={12} />{item.address || item.addr || (item.rating ? `评分 ${Number(item.rating).toFixed(1)}` : "已同步地图位置")}</em>
            </div><UiIcon className="rd-stop-chevron" name="arrow-right" size={16} />
          </div>
        </article>
        {next && <div className="rd-transfer"><button className={activeNavKey === navKey ? "active" : ""} disabled={!item.location || !next.location} onClick={() => {
          if (!item.location || !next.location) return;
          onNav(activeNavKey === navKey ? null : navKey, { from: item.location, to: next.location });
        }}><UiIcon name="navigation" size={13} />前往下一站 <span>{next.dist ? `${next.dist} km` : "查看路线"}</span></button></div>}
      </React.Fragment>;
    })}
    <div className="rd-route-end"><span><UiIcon name="check" size={14} /></span><div><strong>今天的最后一站</strong><small>把夜晚留给你自己</small></div></div>
  </div>;
}

function DetailPhoto({ photo, name }) {
  const [failed, setFailed] = React.useState(null);
  return <div className="rd-photo">{photo && failed !== photo
    ? <img src={photo} alt={name} loading="lazy" onError={() => setFailed(photo)} />
    : <span><UiIcon name="camera" size={22} />{name} · 暂无可用图片</span>}</div>;
}

function DetailCover({ plan, displayDateRange }) {
  const photo = plan.days?.[0]?.items?.find(item => item.type === "attraction")?.photo;
  return <section className="rd-plan-cover" aria-label="行程封面">
    <DetailPhoto photo={photo} name={`${plan.destination}行程封面`} />
    <div className="rd-cover-copy"><small>ITINERARY · 行程总览</small><h2>{plan.title}</h2>
      {!!plan.badges?.length && <p>{plan.badges.map((badge,index) => <span key={index}>{badge}</span>)}</p>}
      <small>{displayDateRange}</small>
      {plan._raw?.transport_summary && <div className="rd-transport-total">
        <small>景点间交通：预计 {plan._raw.transport_summary.minutes} 分钟{plan._raw.transport_summary.cost_cents != null ? ` · 约 ¥${(plan._raw.transport_summary.cost_cents / 100).toFixed(2)}` : " · 部分费用未获取"}；不含餐厅、住宿路段。</small>
        {!!plan._raw.transport_summary.warnings?.length && <small>部分交通方式未获取，仅比较已有可用方案。</small>}
      </div>}
    </div>
  </section>;
}

function detailPhotoUrls(item) {
  const group = (Array.isArray(item?.photos) ? item.photos : []).filter(url => typeof url === "string" && /^(https?:\/\/|\/(?!\/))/.test(url));
  return [...new Set(group.length ? group : item?.photo ? [item.photo] : [])];
}

function DetailPhotoCarousel({ item, city }) {
  const [photos, setPhotos] = React.useState(() => detailPhotoUrls(item));
  const [index, setIndex] = React.useState(0);
  const [status, setStatus] = React.useState("");
  React.useEffect(() => {
    if (!item?.name || photos.length > 1 || (!item.poi_id && !item.location)) return;
    let alive = true;
    setStatus("正在补取当前地点图片…");
    searchPoi(city, item.name, item.type === "attraction" ? "attraction" : "restaurant").then(results => {
      if (!alive) return;
      const matches = results.filter(poi => item.poi_id ? poi.id === item.poi_id : poi.name === item.name && poi.location && item.location && haversineKm(poi.location, item.location) <= 0.1);
      if (matches.length === 1) {
        const group = detailPhotoUrls(matches[0]);
        if (group.length) { setPhotos(group); setIndex(0); }
      }
      setStatus("");
    }).catch(() => { if (alive) setStatus("暂时无法补取更多照片，保留已有图片。"); });
    return () => { alive = false; };
  }, []);
  return <section className="rd-gallery" aria-label="当天景点图片"><h3>当前安排的图片</h3><figure className="rd-photo-carousel">
    <DetailPhoto key={photos[index]} photo={photos[index]} name={item?.name || "当前安排"} />
    <figcaption>{item?.name || "暂无安排"}</figcaption>
    <div className="rd-photo-navigation"><button aria-label="上一张照片" disabled={photos.length < 2} onClick={() => setIndex(value => (value + photos.length - 1) % photos.length)}><UiIcon name="arrow-right" size={16} /></button><span aria-live="polite">{photos.length ? `${index + 1} / ${photos.length}` : "暂无图片"}</span><button aria-label="下一张照片" disabled={photos.length < 2} onClick={() => setIndex(value => (value + 1) % photos.length)}><UiIcon name="arrow-right" size={16} /></button></div>
  </figure>{status && <p className="rd-photo-status" role="status">{status}</p>}</section>;
}

function RedesignCopilot({ plan, planId, day, dayIdx, cover, currentUsername, onBack, onCollapse, detailsVisible, onPlanResult }) {
  const [query, setQuery] = React.useState("");
  const [activeId, setActiveId] = React.useState(null);
  const [state, setState] = React.useState(() => ChatState.initialState());
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState("");
  const [historyOpen, setHistoryOpen] = React.useState(false);
  const abortsRef = React.useRef({});
  const aliveRef = React.useRef(true);
  const activeIdRef = React.useRef(activeId);
  const stateRef = React.useRef(state);
  activeIdRef.current = activeId;
  stateRef.current = state;
  const scrollRef = React.useRef(null);
  const runs = Object.values(state.runs || {}).sort((a, b) => String(a.created_at || "").localeCompare(String(b.created_at || "")));
  const blockingRun = [...runs].reverse().find(run => ["queued", "running", "waiting_user"].includes(run.status)) || null;
  const failedRun = [...runs].reverse().find(run => run.status === "failed") || null;
  const messages = Object.values(state.messages || {}).sort((a, b) => (a.sequence || 0) - (b.sequence || 0));

  const applyRunEvent = React.useCallback((runId, event) => {
    if (!aliveRef.current) return;
    setState(previous => ChatState.applyEvent(previous, runId, event));
    if (event.payload?.kind === "run.status" && event.payload.status === "succeeded") {
      getRun(runId).then(run => { if (aliveRef.current && run?.result_itinerary_id) onPlanResult?.(run.result_itinerary_id); }).catch(() => {});
    }
  }, [onPlanResult]);

  const subscribeRun = React.useCallback(run => {
    if (!run?.id || abortsRef.current[run.id]) return;
    let activeAbort = null;
    streamRuntimeRun(run.id, 0, {
      onAbort: abort => { if (!aliveRef.current) { abort(); return; } activeAbort = abort; abortsRef.current[run.id] = abort; },
      onEvent: event => applyRunEvent(run.id, event),
      onClose: () => { if (abortsRef.current[run.id] === activeAbort) delete abortsRef.current[run.id]; },
      onError: () => { if (abortsRef.current[run.id] === activeAbort) delete abortsRef.current[run.id]; },
    });
  }, [applyRunEvent]);

  React.useEffect(() => {
    aliveRef.current = true;
    Object.values(abortsRef.current).forEach(abort => abort());
    abortsRef.current = {};
    setState(ChatState.initialState()); setActiveId(null); setError(""); setLoading(true);
    if (!currentUsername || !planId) { setLoading(false); return undefined; }
    let alive = true;
    activeIdRef.current = null;
    let refreshing = false;
    const refresh = async () => {
      if (!alive || refreshing || !ChatState.shouldPollConversations(document.visibilityState)) return;
      refreshing = true;
      try {
        let conversationId = activeIdRef.current;
        if (!conversationId) {
          const conversations = await listConversations(planId);
          if (!alive) return;
          // A send can create the first conversation while this list is in flight.
          conversationId = activeIdRef.current || (conversations.find(item => item.status !== "archived") || conversations[0])?.id;
          if (!conversationId) return;
          activeIdRef.current = conversationId;
          setActiveId(conversationId);
        }
        const snapshot = stateRef.current;
        const [loadedMessages, loadedRuns] = await Promise.all([getConversationMessages(conversationId), listRuns(conversationId)]);
        if (!alive || conversationId !== activeIdRef.current) return;
        setState(previous => {
          let next = previous;
          loadedMessages.forEach(message => {
            if (previous.messages[message.id] === snapshot.messages[message.id]) next = ChatState.upsertMessage(next, message);
          });
          const runs = { ...next.runs };
          loadedRuns.forEach(run => {
            // Live events and accepted sends take precedence over older reads.
            if (previous.runs[run.id] === snapshot.runs[run.id]) runs[run.id] = run;
          });
          return { ...next, runs };
        });
        loadedRuns.filter(run => ["queued", "running", "waiting_user"].includes(run.status)
          && (stateRef.current.runs[run.id] === snapshot.runs[run.id]
            || ["queued", "running", "waiting_user"].includes(stateRef.current.runs[run.id]?.status))).forEach(subscribeRun);
        setError("");
      } catch (errorValue) {
        if (alive) setError(errorValue.message || "加载行程对话失败");
      } finally {
        refreshing = false;
        if (alive) setLoading(false);
      }
    };
    refresh();
    const timer = window.setInterval(refresh, 4000);
    document.addEventListener("visibilitychange", refresh);
    return () => {
      alive = false; aliveRef.current = false;
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", refresh);
      Object.values(abortsRef.current).forEach(abort => abort()); abortsRef.current = {};
    };
  }, [planId, currentUsername]); // eslint-disable-line react-hooks/exhaustive-deps

  React.useEffect(() => {
    if (messages.length || runs.length) scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages.length, runs.map(run => `${run.id}:${run.status}`).join("|")]);

  const send = async () => {
    const text = query.trim();
    if (!text || (blockingRun && blockingRun.status !== "waiting_user")) return;
    setQuery(""); setError("");
    try {
      if (blockingRun?.status === "waiting_user") {
        const interactionId = blockingRun.pending_interaction?.interaction_id || blockingRun.outstanding_interaction_id;
        if (!interactionId) throw new Error("当前任务还不能接收回复");
        const resumed = await resumeRuntimeRun(blockingRun.id, interactionId, text);
        if (!aliveRef.current) return;
        setState(previous => {
          let next = previous;
          if (resumed.accepted_message) next = ChatState.upsertMessage(next, resumed.accepted_message);
          return { ...next, runs: { ...next.runs, [blockingRun.id]: { ...next.runs[blockingRun.id], ...resumed } } };
        });
        abortsRef.current[blockingRun.id]?.(); delete abortsRef.current[blockingRun.id]; subscribeRun({ ...blockingRun, ...resumed });
        return;
      }
      let conversationId = activeId;
      if (!conversationId) { const created = await createConversation(`${plan.destination || "旅行"} · 行程调整`); if (!aliveRef.current) return; conversationId = created.id; activeIdRef.current = created.id; setActiveId(created.id); }
      const result = await submitConversationMessage(conversationId, text, { related_itinerary_id: planId });
      if (!aliveRef.current) return;
      setState(previous => {
        const next = ChatState.upsertMessage(previous, result.message);
        return { ...next, runs: { ...next.runs, [result.run.id]: result.run } };
      });
      subscribeRun(result.run);
    } catch (errorValue) { if (aliveRef.current) { setQuery(text); setError(errorValue.message || "发送失败，请重试"); } }
  };

  const controlRun = async (run, action) => {
    try {
      const updated = action === "cancel" ? await cancelRuntimeRun(run.id) : await retryRuntimeRun(run.id);
      if (!aliveRef.current) return;
      setState(previous => ({ ...previous, runs: { ...previous.runs, [updated.id]: updated } }));
      if (action === "retry") subscribeRun(updated);
    } catch (errorValue) { if (aliveRef.current) setError(errorValue.message || "操作失败"); }
  };
  const prompts = [`Day ${dayIdx + 1} 行程轻松一点`, `优化 Day ${dayIdx + 1} 的路线`, day?.theme ? `围绕“${day.theme}”换一个景点` : "换一个更合适的景点"];

  return <section className="rd-chat-panel">
    <div className="rd-chat-head"><div className="rd-breadcrumb"><button onClick={onBack} aria-label="返回"><UiIcon name="arrow-right" size={17} /></button><span>我的行程</span><b>/</b><em>{plan.destination} · {plan.days.length} 日</em></div><div className="rd-chat-actions"><button onClick={() => setHistoryOpen(value => !value)} aria-expanded={historyOpen}><UiIcon name="menu" size={14} />历史对话</button><button className="rd-collapse" onClick={onCollapse} aria-expanded={detailsVisible} aria-controls="itinerary-detail"><UiIcon name="arrow-right" size={14} />{detailsVisible ? "收起详情" : "显示详情"}</button></div></div>
    {cover}
    {historyOpen && <aside className="rd-history-drawer"><header><div><small>YOUR CONVERSATIONS</small><h3>历史对话</h3></div><button aria-label="关闭历史对话" onClick={() => setHistoryOpen(false)}><UiIcon name="close" size={16} /></button></header><strong>{plan.destination} · {plan.days.length} 日游</strong>{loading ? <p>正在恢复关联对话…</p> : !messages.length ? <p>当前行程暂无对话记录</p> : messages.map(message => <article key={message.id}><small>{message.role === "user" ? "你" : "旅行助手"}</small><p>{message.content}</p></article>)}</aside>}
    <div className="rd-conversation" ref={scrollRef} role="log" aria-label="行程调整对话"><div className="rd-conversation-label"><span>CONVERSATION</span><em>AI 已了解右侧行程</em></div><div className="rd-message assistant"><span className="rd-ai-avatar"><Mascot size={25} pose="idle" /></span><div><small>旅行助手</small><p>你好，{plan.username}。我已经把 <b>{plan.destination}</b> 的行程整理好了。告诉我你的偏好，我会一边聊天，一边替你调整右侧路线。</p><em>已结合地图上下文</em></div></div>{messages.map(message => <div className={`rd-message ${message.role === "user" ? "user" : "assistant"}`} key={message.id}>{message.role !== "user" && <span className="rd-ai-avatar"><Mascot size={25} pose="idle" /></span>}<div>{message.role !== "user" && <small>旅行助手</small>}<p>{message.content}</p><em>{message.role === "user" ? "刚刚" : "已结合地图上下文"}</em></div></div>)}
      {blockingRun && <div className="rd-run-card"><header><span className="rd-spinner" /><b>{blockingRun.status === "waiting_user" ? "需要你的回复" : "正在调整当前行程"}</b></header><p>{ChatState.runStatusText(blockingRun)}</p>{blockingRun.status !== "waiting_user" && <button onClick={() => controlRun(blockingRun, "cancel")}>停止</button>}</div>}
      {!blockingRun && failedRun && <div className="rd-run-card failed"><header><UiIcon name="alert" size={15} /><b>这次修改没有完成</b></header><p>{failedRun.error_public?.message || "可以保留输入并重新尝试。"}</p><button onClick={() => controlRun(failedRun, "retry")}>重新尝试</button></div>}
      {loading && <div className="rd-chat-status">正在恢复关联对话…</div>}{error && <div className="rd-chat-status error" role="alert">{error}</div>}
    </div>
    <div className="rd-suggestions">{prompts.map(prompt => <button key={prompt} onClick={() => setQuery(prompt)}>{prompt}</button>)}</div>
    <div className="rd-composer"><textarea value={query} onChange={event => setQuery(event.target.value)} disabled={!!blockingRun && blockingRun.status !== "waiting_user"} onKeyDown={event => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); send(); } }} placeholder={blockingRun?.status === "waiting_user" ? "回答上方问题，继续这次修改…" : blockingRun ? "行程调整中…" : "告诉我你想怎么调整这趟旅程…"} /><footer><span>Enter 发送 · Shift + Enter 换行</span><button onClick={blockingRun && blockingRun.status !== "waiting_user" ? () => controlRun(blockingRun, "cancel") : send} disabled={blockingRun?.status === "waiting_user" ? !query.trim() : !blockingRun && !query.trim()} aria-label="发送"><UiIcon name={blockingRun && blockingRun.status !== "waiting_user" ? "stop" : "arrow-up"} size={17} /></button></footer></div>
    <footer className="rd-chat-footer"><span><i />旅行助手在线</span><em>AI 会根据你的偏好实时更新右侧路线</em></footer>
  </section>;
}

function DetailProfile({ username, onClose }) {
  const [profile, setProfile] = React.useState(null);
  const [failed, setFailed] = React.useState(false);
  const [attempt, setAttempt] = React.useState(0);
  React.useEffect(() => {
    let alive = true;
    setProfile(null); setFailed(false);
    Promise.resolve().then(() => getProfile()).then(data => {
      if (!alive) return;
      if (!data) { setFailed(true); return; }
      setProfile(data);
    }).catch(() => { if (alive) setFailed(true); });
    return () => { alive = false; };
  }, [username, attempt]);
  return <div className="rd-profile-backdrop" onMouseDown={onClose}><section className="rd-profile" role="dialog" aria-modal="true" aria-label="旅行画像" onMouseDown={event => event.stopPropagation()}><header><div><small>MY PROFILE · 我的画像</small><h2>{username}的旅行画像</h2></div><button aria-label="关闭旅行画像" onClick={onClose}><UiIcon name="close" size={17} /></button></header>
    {failed ? <div role="alert">画像加载失败，请稍后重试。<button onClick={() => setAttempt(value => value + 1)}>重新加载画像</button></div> : !profile ? <p role="status">正在加载旅行画像…</p> : <>
      <div className="rd-profile-id"><span>{String(username || "旅").slice(0, 1)}</span><div><strong>{profile.active_facts.length} 条已确认记忆</strong><p>{profile.trip_count} 趟旅程 · 记忆版本 {profile.revision}</p></div><em>{profile.candidate_facts.length} 条待确认</em></div>
      <h4>已确认的旅行记忆</h4><div className="rd-profile-tags">{profile.active_facts.length ? profile.active_facts.map(fact => <span key={fact.id}>{MEMORY_POLARITY_LABELS[fact.polarity] || fact.polarity}：{fact.value_text} · {MEMORY_SCOPE_LABELS[fact.scope_type] || fact.scope_type}{Object.values(fact.scope_key || {}).filter(Boolean).map(value => ` · ${value}`).join("")}</span>) : <p>还没有已确认的旅行记忆。</p>}</div>
      <div className="rd-profile-note"><UiIcon name="sparkle" size={16} /><p>可在“我的画像”中编辑记忆、确认候选或忘记不再适用的信息。</p></div>
    </>}
  </section></div>;
}

function TripDetailPage(props) {
  return <TripDetailWorkspace key={JSON.stringify([props.currentUsername, props.planId])} {...props} />;
}

function TripDetailWorkspace({ plan: planProp, planId: planIdProp, onRequestModify, currentUsername, onBack, onPlanChange }) {
  const [plan, setPlan] = React.useState(planProp);
  const [planId, setPlanId] = React.useState(planIdProp);
  const [dayIdx, setDayIdx] = React.useState(0);
  const [selectedId, setSelectedId] = React.useState(null);
  const [activeNavKey, setActiveNavKey] = React.useState(null);
  const [activeNavPair, setActiveNavPair] = React.useState(null);
  const [detailWidth, setDetailWidth] = React.useState(54);
  const [resizing, setResizing] = React.useState(false);
  const [chatOnly, setChatOnly] = React.useState(false);
  const [detailOnly, setDetailOnly] = React.useState(false);
  const [profileOpen, setProfileOpen] = React.useState(false);
  const [notice, setNotice] = React.useState("");
  const [editing, setEditing] = React.useState(false);
  const dayTabRefs = React.useRef([]);
  const aliveRef = React.useRef(true);
  React.useEffect(() => { aliveRef.current = true; return () => { aliveRef.current = false; }; }, []);
  React.useEffect(() => { dayTabRefs.current[dayIdx]?.scrollIntoView?.({ block: "nearest", inline: "nearest" }); }, [dayIdx, planId]);

  React.useEffect(() => {
    setPlan(planProp); setPlanId(planIdProp); setDayIdx(0); setSelectedId(null);
  }, [planProp, planIdProp]);

  React.useEffect(() => {
    if (!resizing) return undefined;
    const move = event => setDetailWidth(Math.min(70, Math.max(40, ((window.innerWidth - event.clientX) / window.innerWidth) * 100)));
    const stop = () => setResizing(false);
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", stop, { once: true });
    return () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", stop); };
  }, [resizing]);

  React.useEffect(() => {
    if (!planId) return undefined;
    let abort = null; let alive = true;
    streamItineraryTips(planId, {
      onAbort: fn => { if (!alive) { fn(); return; } abort = fn; },
      onEvent: event => {
        if (event.payload?.kind !== "itinerary.tip_status_changed") return;
        getHistoryItem(planId).then(data => {
          if (alive && data?.plan) setPlan(adaptPlan(data.plan, currentUsername));
        }).catch(() => {});
      },
    });
    return () => { alive = false; abort?.(); };
  }, [planId, currentUsername]);

  if (!plan?.days?.length) return null;
  const day = plan.days[dayIdx] || plan.days[0];
  const selectedIndex = Math.max(0, day.items?.findIndex(item => item.selectionId === selectedId) ?? 0);
  const setSelectedIndex = index => { setSelectedId(day.items?.[index]?.selectionId || null); setActiveNavKey(null); setActiveNavPair(null); };
  const selected = day.items?.[selectedIndex] || day.items?.[0] || null;
  const weather = plan.weather?.[dayIdx] || plan.weather?.[0];
  const displayDateRange = (() => {
    const start = plan._raw?.start_date;
    const end = plan._raw?.end_date;
    if (!start || !end) return plan.date_range || "日期待定";
    const [, sm, sd] = start.split("-");
    const [, em, ed] = end.split("-");
    return `${Number(sm)} 月 ${Number(sd)} 日 — ${Number(em)} 月 ${Number(ed)} 日`;
  })();
  const showNotice = text => { setNotice(text); window.setTimeout(() => setNotice(""), 2200); };
  const sharePlan = async () => {
    if (!planId) return;
    try {
      const target = new URL("/", window.location.href);
      target.searchParams.set("view_plan_id", planId);
      await navigator.clipboard.writeText(target.href);
      showNotice("分享链接已复制");
    } catch { showNotice("复制失败，请允许剪贴板访问后重试。"); }
  };
  const openConversationResult = async resultPlanId => {
    if (!resultPlanId) return;
    try {
      const data = await getHistoryItem(resultPlanId);
      if (!aliveRef.current || !data?.plan) return;
      const adapted = adaptPlan(data.plan, currentUsername);
      setPlan(adapted); setPlanId(resultPlanId); setDayIdx(0); setSelectedId(null);
      onPlanChange?.(data.plan, resultPlanId);
    } catch {}
  };
  const changeDay = index => {
    setDayIdx(index); setSelectedId(null); setActiveNavKey(null); setActiveNavPair(null);
  };

  if (editing) return <LegacyTripDetailPage plan={editing.plan} planId={editing.planId} currentUsername={currentUsername} initialEditing onRequestModify={onRequestModify} onBack={() => setEditing(false)} onExitEditing={() => setEditing(false)} onPlanChange={(raw, id) => {
    if (!aliveRef.current) return;
    setPlan(adaptPlan(raw, currentUsername)); setPlanId(id); setEditing(false); onPlanChange?.(raw, id);
  }} />;

  return <div className="page page-fade trip-detail-page rd-page">
    <main className="rd-app">
      <header className="rd-top-nav">
        <div className="rd-brand"><span className="rd-brand-mark"><i /></span><strong>WAygent · AI 旅行规划</strong></div>
        <div className="rd-studio"><UiIcon name="sparkle" size={14} />AI TRIP STUDIO <span>·</span> {plan.destination} / {(plan._raw?.start_date || "").slice(0, 4) || "2026"}</div>
        <div className="rd-nav-actions"><button onClick={() => showNotice("行程已保存")} aria-label="保存行程"><UiIcon name="check" size={16} /></button><button className="rd-avatar" onClick={() => setProfileOpen(true)}>{String(plan.username || currentUsername || "旅").slice(0, 1)}</button></div>
      </header>

      <section className={`rd-workspace${chatOnly ? " chat-only" : ""}${detailOnly ? " detail-only" : ""}`} style={{ "--rd-detail-width": `${detailWidth}%` }}>
        <RedesignCopilot key={JSON.stringify([currentUsername, planId])} plan={plan} planId={planId} day={day} dayIdx={dayIdx} cover={<DetailCover plan={plan} displayDateRange={displayDateRange} />} currentUsername={currentUsername} onBack={onBack} detailsVisible={!chatOnly} onCollapse={() => { setChatOnly(value => !value); setDetailOnly(false); }} onPlanResult={openConversationResult} />
        <div className="rd-divider" role="separator" tabIndex="0" aria-label="调整对话和行程详情宽度" aria-orientation="vertical" aria-valuemin="40" aria-valuemax="70" aria-valuenow={Math.round(detailWidth)} onPointerDown={event => { event.preventDefault(); setResizing(true); }} onDoubleClick={() => setDetailWidth(54)} onKeyDown={event => {
          if (event.key === "Home") setDetailWidth(54);
          if (event.key === "ArrowLeft") setDetailWidth(value => Math.min(70, value + 2));
          if (event.key === "ArrowRight") setDetailWidth(value => Math.max(40, value - 2));
        }}><span /></div>

        <section className="rd-detail-panel" id="itinerary-detail" hidden={chatOnly}>
          <div className="rd-detail-header">
            <div className="rd-detail-title"><div><small>TRIP DETAIL / {plan.destination}</small><h2>路线详情</h2></div><div><button disabled={!planId || !plan._raw?.days} onClick={() => setEditing({ plan, planId })}>编辑行程</button><button onClick={() => { setDetailOnly(value => !value); setChatOnly(false); }}><UiIcon name="arrow-right" size={14} />{detailOnly ? "显示对话" : "收起对话"}</button><button className="primary" disabled={!planId} onClick={sharePlan}>分享行程 <UiIcon name="share" size={14} /></button></div></div>
            <nav className="rd-day-tabs" role="tablist" aria-label="选择行程日期">{plan.days.map((item, index) => <button key={index} ref={node => { dayTabRefs.current[index] = node; }} role="tab" tabIndex={dayIdx === index ? 0 : -1} aria-selected={dayIdx === index} className={dayIdx === index ? "active" : ""} onClick={() => changeDay(index)} onKeyDown={event => {
              const target = event.key === "End" ? plan.days.length - 1 : event.key === "Home" ? 0
                : event.key === "ArrowRight" ? (index + 1) % plan.days.length : event.key === "ArrowLeft" ? (index + plan.days.length - 1) % plan.days.length : null;
              if (target === null) return;
              event.preventDefault(); changeDay(target); dayTabRefs.current[target]?.focus();
            }}><span>{String(index + 1).padStart(2, "0")}</span><div><strong>Day {index + 1}</strong><small>{item.date}</small></div><i /></button>)}</nav>
          </div>

          <div className="rd-detail-body">
            <section className="rd-route-column"><header><div><small>DAY · {String(dayIdx + 1).padStart(2, "0")}</small><h3>{day.theme || `Day ${dayIdx + 1} 的旅行安排`}</h3></div><span>{day.items.length} 项安排</span></header>
              <RedesignTimeline items={day.items} selectedIndex={selectedIndex} onSelect={setSelectedIndex} activeNavKey={activeNavKey} tipStatus={plan.tip_status} onNav={(key, pair) => { setActiveNavKey(key); setActiveNavPair(key ? pair : null); }} />
            </section>

            <section className="rd-media-column"><DetailPhotoCarousel key={JSON.stringify([currentUsername,planId,dayIdx,selectedIndex,selected?.poi_id,selected?.name,selected?.location,selected?.photo,selected?.photos])} item={selected} city={plan.destination} /><section className="rd-map-column" aria-label={`Day ${dayIdx + 1} 路线地图`}>
              <MapPanel key={`${planId}:${dayIdx}`} day={day} dayIdx={dayIdx} workbenchControls realOnly selectedId={selected?.selectionId || null} onPointSelect={id => { const index = day.items.findIndex(item => item.selectionId === id); if (index >= 0) setSelectedIndex(index); }} navPair={activeNavPair} onNavClear={() => { setActiveNavKey(null); setActiveNavPair(null); }} />
              {selected && <div className="rd-map-card"><small>NOW VIEWING</small><strong>{selected.name}</strong><span>{selected.address || selected.addr || (selected.type === "attraction" ? "已同步地图位置" : "本地餐饮")} · {itemDuration(selected)}</span><div><em><UiIcon name="navigation" size={13} />当前地点</em><em><WeatherGlyph value={weather?.icon || weather?.text} size={13} />{weather?.hi || "—"}° {weather?.text || ""}</em></div></div>}
            </section></section>
          </div>
        </section>
        {detailOnly && <button className="rd-reopen chat" onClick={() => setDetailOnly(false)}>显示 AI 对话</button>}
      </section>

      {profileOpen && <DetailProfile username={currentUsername || plan.username || "旅行者"} onClose={() => setProfileOpen(false)} />}
      {notice && <div className="rd-toast" role="status"><UiIcon name="check" size={14} />{notice}</div>}
    </main>
  </div>;
}

Object.assign(window, { TripDetailPage });

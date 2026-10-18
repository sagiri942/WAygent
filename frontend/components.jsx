// components.jsx — 统一图标 / 卡片 / 地图 / 旅程加载动画

/* ── 小鸭品牌线性图标 ────────────────────────────── */
const UI_ICON_PATHS = {
  plane: <><path d="M3 11.5 21 5l-2 4-6 3 4 2-2 2-5-2-3 4-2-1 1-5-3-1z"/><path d="m10 9-3-5 2-1 5 4"/></>,
  train: <><rect x="5" y="3" width="14" height="16" rx="3"/><path d="M8 7h8M8 12h.01M16 12h.01M8 19l-2 2m10-2 2 2"/></>,
  car: <><path d="m5 16-1-3 2-5h12l2 5-1 3z"/><path d="M7 8l1-3h8l1 3M7 16v2m10-2v2M7 13h.01M17 13h.01"/></>,
  bed: <><path d="M3 19V5M3 14h18v5M7 14V9h7a4 4 0 0 1 4 4v1M3 9h4v5"/></>,
  camera: <><path d="M4 7h4l2-2h4l2 2h4v12H4z"/><circle cx="12" cy="13" r="3.5"/></>,
  location: <><path d="M20 10c0 5-8 11-8 11S4 15 4 10a8 8 0 1 1 16 0Z"/><circle cx="12" cy="10" r="2.5"/></>,
  heart: <path d="M20.8 5.8a5 5 0 0 0-7.1 0L12 7.5l-1.7-1.7a5 5 0 0 0-7.1 7.1L12 21l8.8-8.1a5 5 0 0 0 0-7.1Z"/>,
  bookmark: <path d="M6 3h12v18l-6-4-6 4z"/>,
  sun: <><circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M4.9 4.9l1.4 1.4m11.4 11.4 1.4 1.4M2 12h2m16 0h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></>,
  moon: <path d="M20 15.2A8.5 8.5 0 0 1 8.8 4a8.5 8.5 0 1 0 11.2 11.2Z"/>,
  cloud: <path d="M5 18a4 4 0 0 1 0-8 7 7 0 0 1 13.5 2A3.5 3.5 0 1 1 19 19H5Z"/>,
  rain: <><path d="M5 15a4 4 0 0 1 0-8 7 7 0 0 1 13.5 2A3.5 3.5 0 1 1 19 16H5Z"/><path d="m8 19-1 2m6-2-1 2m6-2-1 2"/></>,
  snow: <><path d="M5 14a4 4 0 0 1 0-8 7 7 0 0 1 13.5 2A3.5 3.5 0 1 1 19 15H5Z"/><path d="M8 19h.01M13 20h.01M18 19h.01"/></>,
  leaf: <><path d="M20 4C12 4 5 8 5 15c4 2 10 1 13-3 2-3 2-8 2-8Z"/><path d="M4 20c3-5 7-8 13-11"/></>,
  sparkle: <><path d="m12 2 1.4 4.6L18 8l-4.6 1.4L12 14l-1.4-4.6L6 8l4.6-1.4z"/><path d="m19 14 .8 2.2L22 17l-2.2.8L19 20l-.8-2.2L16 17l2.2-.8z"/></>,
  utensils: <><path d="M7 3v7m-3-7v4a3 3 0 0 0 6 0V3M7 10v11M17 3v18M17 3c3 3 3 7 0 10"/></>,
  map: <><path d="m3 6 6-3 6 3 6-3v15l-6 3-6-3-6 3z"/><path d="M9 3v15m6-12v15"/></>,
  navigation: <><path d="m20 4-7 16-2-7-7-2z"/><path d="m11 13 4-4"/></>,
  edit: <><path d="M4 20h4L19 9l-4-4L4 16z"/><path d="m13.5 6.5 4 4"/></>,
  shuffle: <><path d="M3 7h3c5 0 7 10 12 10h3"/><path d="m18 14 3 3-3 3M3 17h3c2 0 3.5-1.6 5-4M14 7c1.2 0 2.4 0 4 0h3m-3-3 3 3-3 3"/></>,
  undo: <><path d="m9 7-5 5 5 5"/><path d="M4 12h9a6 6 0 0 1 6 6"/></>,
  redo: <><path d="m15 7 5 5-5 5"/><path d="M20 12h-9a6 6 0 0 0-6 6"/></>,
  close: <path d="m6 6 12 12M18 6 6 18"/>,
  plus: <path d="M12 5v14M5 12h14"/>,
  minus: <path d="M5 12h14"/>,
  "arrow-right": <><path d="M5 12h14"/><path d="m14 7 5 5-5 5"/></>,
  "arrow-up": <><path d="M12 19V5"/><path d="m7 10 5-5 5 5"/></>,
  stop: <rect x="6" y="6" width="12" height="12" rx="2"/>,
  check: <path d="m5 12 4 4L19 6"/>,
  "chevron-up": <path d="m6 15 6-6 6 6"/>,
  "chevron-down": <path d="m6 9 6 6 6-6"/>,
  star: <path d="m12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2-5.6-3-5.6 3 1.1-6.2L3 9.6l6.2-.9z"/>,
  wallet: <><path d="M4 6h14a2 2 0 0 1 2 2v11H4z"/><path d="M4 6V4h12v2M15 12h5"/><circle cx="16" cy="12" r=".5"/></>,
  clock: <><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></>,
  phone: <path d="M6 3h4l1 5-2 1c1 3 3 5 6 6l1-2 5 1v4c0 2-2 3-4 3C9 19 5 15 3 7c0-2 1-4 3-4Z"/>,
  flask: <><path d="M9 3h6M10 3v6l-5 9a2 2 0 0 0 2 3h10a2 2 0 0 0 2-3l-5-9V3"/><path d="M8 15h8"/></>,
  alert: <><path d="m12 3 10 18H2z"/><path d="M12 9v5m0 3h.01"/></>,
  success: <><circle cx="12" cy="12" r="9"/><path d="m8 12 3 3 5-6"/></>,
  failure: <><circle cx="12" cy="12" r="9"/><path d="m9 9 6 6m0-6-6 6"/></>,
  archive: <><path d="M4 7h16v13H4zM3 3h18v4H3z"/><path d="M9 11h6"/></>,
  compress: <><path d="M5 7h14M7 12h10M9 17h6"/></>,
  menu: <path d="M4 7h16M4 12h16M4 17h16"/>,
  share: <><circle cx="18" cy="5" r="2.5"/><circle cx="6" cy="12" r="2.5"/><circle cx="18" cy="19" r="2.5"/><path d="m8.2 10.8 7.6-4.5M8.2 13.2l7.6 4.5"/></>,
};

function UiIcon({ name, size = 18, strokeWidth = 2, label = null, className = "" }) {
  const content = UI_ICON_PATHS[name] || UI_ICON_PATHS.sparkle;
  return (
    <svg
      className={`ui-icon ${className}`.trim()}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={strokeWidth}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden={label ? undefined : "true"}
      role={label ? "img" : undefined}
      aria-label={label || undefined}
      focusable="false"
    >
      {content}
    </svg>
  );
}

function WeatherGlyph({ value, size = 26 }) {
  const glyph = String(value || "");
  const name = /🌧|🌦|⛈|雨/.test(glyph) ? "rain"
    : /❄|🌨|雪/.test(glyph) ? "snow"
    : /☁|🌥|⛅|云|阴/.test(glyph) ? "cloud"
    : /🌙|夜/.test(glyph) ? "moon"
    : /☀|🌤|晴/.test(glyph) ? "sun"
    : null;
  return name
    ? <UiIcon name={name} size={size} label={glyph || "天气"} />
    : <span className="weather-glyph-fallback" aria-label="天气">{glyph || "·"}</span>;
}

/* ── 旅程加载动画 ─────────────────────────────── */
// 旅程站点：后端多个节点可映射到同一站（NODE_TO_STEP），
// planner⇄reviewer / planner⇄time_check 多轮循环时小人只前进不后退，
// 轮次细节由实时文案（stage label）展示。
const JOURNEY_STEPS = [
  { key: "weather_lookup",    label: "结合旅行偏好", detail: "正在确认需求与天气", doneDetail: "需求已确认" },
  { key: "attraction_search", label: "检索景点池", detail: "正在寻找真实景点", doneDetail: "候选景点已备好" },
  { key: "optimizer",         label: "编排行程", detail: "正在优化路线与节奏", doneDetail: "路线初稿已完成" },
  { key: "reviewer",          label: "评审路线", detail: "正在检查体验与路线", doneDetail: "路线评审完成" },
  { key: "time_check",        label: "检查开放时间、强度和冲突", detail: "正在核查安排是否可行", doneDetail: "关键冲突已核查" },
  { key: "finalize",          label: "完善餐饮与细节", detail: "正在整理最终版本", doneDetail: "完整行程已生成" },
];
// 后端节点名 → 旅程站点 key
const NODE_TO_STEP = {
  weather_lookup:    "weather_lookup",
  attraction_search: "attraction_search",
  candidate_builder: "attraction_search",
  optimizer:         "optimizer",
  quality_gate:      "optimizer",
  planner:           "optimizer",
  reviewer:          "reviewer",
  time_check:        "time_check",
  meal_search:       "finalize",
  meal_recommend:    "finalize",
  spot_tips:         "finalize",
  finalize:          "finalize",
};
Object.assign(window, { JOURNEY_STEPS, NODE_TO_STEP });

function DayMap({ mapPoints, dayKey }) {
  const pts = mapPoints || [];
  const poly = pts.map((p) => `${p.x},${p.y}`).join(" ");
  // 景点单独编号，餐厅不占号
  let spotNo = 0;
  const spotNums = pts.map(p => (p.kind === "meal" ? null : ++spotNo));

  return (
    <div className="map-canvas">
      <svg viewBox="0 0 100 100" preserveAspectRatio="xMidYMid slice" key={dayKey}>
        <g stroke="var(--line-2)" strokeWidth=".35">
          {[15, 30, 45, 60, 75, 90].map((v) => (
            <React.Fragment key={v}>
              <line x1={v + 4} y1="-5" x2={v - 8} y2="105" />
              <line x1="-5" y1={v} x2="105" y2={v - 6} />
            </React.Fragment>
          ))}
        </g>
        <path d="M-5 84 Q 24 72 46 84 T 105 80 L 105 105 L -5 105 Z" fill="var(--map-water)" />
        <path d="M58 -5 q 10 18 -2 32 q -8 11 2 22" fill="none" stroke="var(--map-water)" strokeWidth="5" strokeLinecap="round" opacity=".8" />
        <ellipse cx="22" cy="22" rx="16" ry="11" fill="var(--map-park)" />
        <ellipse cx="84" cy="38" rx="13" ry="9" fill="var(--map-park)" />
        <ellipse cx="44" cy="62" rx="9" ry="6" fill="var(--map-park)" opacity=".8" />
        {poly && (
          <polyline
            points={poly} fill="none"
            stroke="var(--accent)" strokeWidth=".9"
            strokeDasharray="2 1.9" strokeLinecap="round" strokeLinejoin="round"
            style={{ animation: "routeDash 30s linear infinite" }}
          />
        )}
        <style>{`@keyframes routeDash { to { stroke-dashoffset: -92; } }`}</style>
        {pts.map((p, i) => (
          <g key={`${dayKey}-${i}`} className="map-pin-pop" style={{ "--pin-delay": `${i * 0.12 + 0.1}s`, transformOrigin: `${p.x}px ${p.y}px` }}>
            {p.kind === "meal" ? (
              <g>
                <rect x={p.x - 2.6} y={p.y - 2.6} width="5.2" height="5.2" rx="1.2"
                  fill="var(--second)" transform={`rotate(45 ${p.x} ${p.y})`} />
                <circle cx={p.x} cy={p.y} r=".9" fill="var(--card)" />
              </g>
            ) : (
              <g>
                <circle cx={p.x} cy={p.y} r="3.4" fill="var(--accent)" />
                <text x={p.x} y={p.y + 1.6} textAnchor="middle" fontSize="4.2" fontWeight="800" fill="var(--accent-ink)" fontFamily="var(--font-body)">{spotNums[i]}</text>
              </g>
            )}
            <text className="map-pin-label" x={p.x} y={p.y - 5} textAnchor="middle" style={{ fontSize: "3.4px" }}>{p.label}</text>
          </g>
        ))}
      </svg>
    </div>
  );
}

function MapPanel({ day, dayIdx, navPair, onNavClear, children, workbenchControls = false, realOnly = false, selectedId, onPointSelect }) {
  const amapRef = React.useRef(null);
  const onPointSelectRef = React.useRef(onPointSelect);
  onPointSelectRef.current = onPointSelect;
  // null=尝试加载中（显示 SVG 占位） true=高德地图就绪 false=降级 SVG
  const [amapReady, setAmapReady] = React.useState(null);
  const hasGeo = (day.mapPoints || []).some(hasMapCoordinates);
  const selectedPoint = (day.mapPoints || []).find(p => p.selectionId === selectedId && hasMapCoordinates(p));

  // 点位签名：优化路线后同一天的点位顺序变化时也要重画地图
  const ptsSig = JSON.stringify(day.mapPoints || []);

  React.useEffect(() => {
    if (!hasGeo) { setAmapReady(false); return; }
    let alive = true;
    const node = amapRef.current;
    setAmapReady(null);
    Promise.resolve().then(() => initAmapForDay(node, day.mapPoints, id => {
      if (alive) onPointSelectRef.current?.(id);
    })).then(ok => {
      if (!alive) return;
      setAmapReady(ok);
    }).catch(() => { if (alive) setAmapReady(false); });
    return () => { alive = false; };
  }, [dayIdx, hasGeo, ptsSig]);

  // Keep one SDK instance while switching days; destroy only on unmount.
  React.useEffect(() => {
    const node = amapRef.current;
    return () => destroyAmap(node);
  }, [hasGeo]);

  // 导航对 / 恢复全日路线
  React.useEffect(() => {
    if (amapReady !== true) return;
    if (navPair) {
      drawNavPairRoute(amapRef.current, navPair.from, navPair.to);
    } else {
      restoreFullRoute(amapRef.current, day.mapPoints);
    }
  }, [navPair, amapReady]); // eslint-disable-line

  React.useEffect(() => {
    if (amapReady === true && selectedId !== undefined) selectAmapPoint(amapRef.current, selectedPoint?.selectionId || null);
  }, [selectedId, selectedPoint?.selectionId, amapReady, ptsSig]);

  return (
    <div className="map-frame">
      <div className="map-head">
        <span className="mh-title">Day {dayIdx + 1} 路线图</span>
        {selectedId != null && !selectedPoint && <span className="mh-note" role="status">当前安排暂无可用坐标</span>}
        {navPair && onNavClear && (
          <button className="mh-nav-clear" onClick={onNavClear}><UiIcon name="close" size={14} />退出导航</button>
        )}
        {!realOnly && amapReady !== true && !navPair && <span className="mh-note">地点示意 · 相对位置</span>}
      </div>
      <div className="map-stage">
        {!realOnly && <DayMap mapPoints={day.mapPoints} dayKey={dayIdx} />}
        {realOnly && amapReady !== true && <p className="detail-map-status" role="status">{!hasGeo ? "当天暂无可用坐标" : amapReady === null ? "正在加载路线地图…" : "地图暂不可用，请查看当天行程安排。"}</p>}
        {hasGeo && (
          <div
            ref={amapRef}
            className="amap-host"
            style={{
              position: "absolute", inset: 0,
              opacity: amapReady === true ? 1 : 0,
              pointerEvents: amapReady === true ? "auto" : "none",
              transition: "opacity .4s ease",
            }}
          />
        )}
        {workbenchControls && (
          <div className="detail-map-controls" aria-label="地图控制">
            <button disabled={realOnly && amapReady !== true} onClick={() => controlAmap(amapRef.current, "zoom-in")} aria-label="放大地图"><UiIcon name="plus" size={18} /></button>
            <button disabled={realOnly && amapReady !== true} onClick={() => controlAmap(amapRef.current, "zoom-out")} aria-label="缩小地图"><UiIcon name="minus" size={18} /></button>
            <button disabled={realOnly && amapReady !== true} onClick={() => controlAmap(amapRef.current, "fit")} aria-label="显示完整路线"><UiIcon name="compress" size={17} /></button>
          </div>
        )}
      </div>
      <div className="map-legend">
        <span><span className="legend-dot" style={{ background: "var(--accent)" }}></span>景点</span>
        <span><span className="legend-dot" style={{ background: "var(--second)", borderRadius: 2 }}></span>餐厅</span>
        {!realOnly && amapReady !== true && <span style={{ marginLeft: "auto" }}>虚线为景点游览顺序</span>}
      </div>
      {children}
    </div>
  );
}

/* ── 时间轴卡片 ───────────────────────────────── */
function photoUrl(seed, w = 640, h = 440) {
  return `https://picsum.photos/seed/${encodeURIComponent(seed)}/${w}/${h}`;
}
Object.assign(window, { photoUrl });

function Thumb({ photo, seed, alt }) {
  const [err, setErr] = React.useState(false);
  const src = photo || (seed ? photoUrl(seed, 360, 280) : null);
  if (!src || err) return <div className="t-thumb ph" aria-label={alt}>◌</div>;
  return (
    <div className="t-thumb" style={{ backgroundImage: `url('${src}')` }} aria-label={alt}>
      <img src={src} alt="" style={{ display: "none" }} onError={() => setErr(true)} />
    </div>
  );
}

const PERIOD = {
  morning:   { label: "上午", cls: "morning" },
  afternoon: { label: "下午", cls: "afternoon" },
  evening:   { label: "夜间", cls: "evening" },
};

// 站点间通行标注：≤3km 按步行（约 4km/h）估时，更远建议乘车
function walkNote(dist) {
  if (dist == null || !(dist > 0)) return null;
  const d = Number(dist);
  if (d <= 3) return `步行 ${d} km · 约 ${Math.max(1, Math.round(d * 15))} 分钟`;
  return `相距 ${d} km · 建议乘车`;
}

function WalkIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M13 4a2 2 0 1 0 0-.1M7 21l3-4.5L8 12l2.5-3 2.5 2 3 1" />
    </svg>
  );
}

function formatPoiCost(value) {
  if (value == null || value === "") return "";
  if (Array.isArray(value)) {
    if (!value.length) return "票价未提供";
    value = value[0];
  }
  const raw = String(value).trim();
  if (!raw) return "";
  if (["[]", "null", "none", "未知", "暂无"].includes(raw.toLowerCase())) {
    return "票价未提供";
  }
  if (/^免费$/i.test(raw)) return "免费";
  if (/^0+(?:\.0+)?$/.test(raw)) return "免费";
  if (/^\d+(?:\.\d+)?$/.test(raw)) return `¥${raw}/人`;
  if (/[元¥￥]/.test(raw)) return raw;
  return "票价未提供";
}

function AttractionCard({ item, onNearby, tipStatus }) {
  const p = PERIOD[item.period] || PERIOD.morning;
  const costLabel = formatPoiCost(item.cost);
  return (
    <div className="tl-node">
      <div className="t-card">
        <Thumb photo={item.photo} seed={item.seed} alt={item.name} />
        <div className="t-body">
          <div className="t-title">
            {item.name}
            <span className={`period-tag ${p.cls}`}>{p.label}</span>
          </div>
          <div className="t-meta">
            {item.start && <span>{item.start}{item.end ? ` – ${item.end}` : ""}</span>}
            {item.rating != null && <span className="star"><UiIcon name="star" size={13} /> {Number(item.rating).toFixed(1)}</span>}
            {costLabel && <span><UiIcon name="wallet" size={13} /> {costLabel}</span>}
            {item.open && <span className="t-open">开放 {item.open}</span>}
          </div>
          {item.address && <div className="t-address"><UiIcon name="location" size={13} /> {item.address}</div>}
          {item.tel && <div className="t-address"><UiIcon name="phone" size={13} /> {item.tel}</div>}
          {item.note ? <div className="tip-box"><UiIcon name="sparkle" size={14} />{item.note}</div> :
            (tipStatus === "queued" || tipStatus === "running") ? <div className="tip-box"><UiIcon name="sparkle" size={14} />小way正在整理贴士…</div> :
            tipStatus === "unavailable" ? <div className="tip-box"><UiIcon name="sparkle" size={14} />路线已更新，贴士尚未更新</div> :
            <div className="tip-box"><UiIcon name="sparkle" size={14} />暂未生成贴士</div>}
          {onNearby && item.location && (
            <button className="nearby-btn" onClick={() => onNearby(item)}><UiIcon name="location" size={14} />周边搜索</button>
          )}
        </div>
      </div>
    </div>
  );
}

function MealCard({ item }) {
  const label = item.type === "lunch" ? "午餐" : "晚餐";
  const costLabel = formatPoiCost(item.cost);
  if (item.no_restaurant) {
    return (
      <div className="tl-node is-meal">
        <div className="t-card meal-card">
          <div className="t-body">
            <div className="t-title"><span className="meal-flag">{label}</span>该时段附近暂无餐厅数据</div>
            <div className="t-note">建议出发前自行规划{label}，或让我换一片区域再找找。</div>
          </div>
        </div>
      </div>
    );
  }
  return (
    <div className="tl-node is-meal">
      <div className="t-card meal-card">
        <Thumb photo={item.photo} seed={item.seed} alt={item.name} />
        <div className="t-body">
          <div className="t-title">
            {item.name}
            <span className="meal-flag">{label}</span>
          </div>
          <div className="t-meta">
            {item.rating != null && <span className="star"><UiIcon name="star" size={13} /> {Number(item.rating).toFixed(1)}</span>}
            {costLabel && <span>{costLabel}</span>}
            {item.category && <span>{item.category}</span>}
            {item.addr && <span>{item.addr}</span>}
          </div>
          {item.open && <div className="t-address"><UiIcon name="clock" size={13} /> {item.open}</div>}
          {item.tel && <div className="t-address"><UiIcon name="phone" size={13} /> {item.tel}</div>}
          {item.reason && <div className="reason-box">{item.reason}</div>}
        </div>
      </div>
    </div>
  );
}

// 导航行：夹在任意相邻两个 item 之间
function NavRow({ itemA, itemB, onNav, isActive }) {
  const hasCoords = itemA?.location?.lat && itemA?.location?.lng
    && itemB?.location?.lat && itemB?.location?.lng;
  if (!hasCoords) return null;
  const dist = itemB.dist;
  return (
    <div className="nav-row">
      <div className="tl-when nav-row-dist">{dist ? `${dist}km` : ""}</div>
      <div className="tl-spine nav-row-spine">
        <div className="tl-line" style={{ height: 8 }}></div>
        <div className={`nav-row-node${isActive ? " active" : ""}`}><UiIcon name="navigation" size={14} /></div>
        <div className="tl-line" style={{ flex: 1 }}></div>
      </div>
      <div className="nav-row-content">
        <button
          className={`nav-row-btn${isActive ? " active" : ""}`}
          onClick={() => onNav({ from: itemA.location, to: itemB.location })}
        ><UiIcon name="navigation" size={14} />导航</button>
        <span className="nav-row-label">{itemA.name} → {itemB.name}</span>
      </div>
    </div>
  );
}

// 时间轴：左侧时间槽 + 轴线，相邻两项之间插入导航行
function Timeline({ items, onNav, activeNavKey, tipStatus }) {
  return (
    <div className="timeline">
      {items.map((item, i) => {
        const isMeal = item.type !== "attraction";
        const prevItem = i > 0 ? items[i - 1] : null;
        const navKey = i > 0 ? String(i) : null;
        return (
          <React.Fragment key={i}>
            {i > 0 && onNav && (
              <NavRow
                itemA={prevItem}
                itemB={item}
                onNav={(pair) => onNav(activeNavKey === navKey ? null : navKey, pair)}
                isActive={activeNavKey === navKey}
              />
            )}
            <div className="tl-row">
              <div className="tl-when">{isMeal ? "" : (item.start || "")}</div>
              <div className="tl-spine tl-spine-dot"><span className={`tl-dot${isMeal ? " meal" : ""}`}></span></div>
              {isMeal
                ? <MealCard item={item} />
                : <AttractionCard item={item} tipStatus={tipStatus} onNearby={onNav ? (it) => onNav("nearby:" + i, null, it) : undefined} />
              }
            </div>
          </React.Fragment>
        );
      })}
    </div>
  );
}

/* ── 候选景点推荐横滚条 ──────────────────────────── */
function RecommendStrip({ candidates, editing }) {
  const [detail, setDetail] = React.useState(null);

  if (!candidates || !candidates.length) return null;
  return (
    <div className="recommend-strip">
      <div className="recommend-label">
        <UiIcon name="map" size={15} />LLM 候选景点池{editing ? " · 拖入时间轴可替换景点" : ""}
      </div>
      <div className="recommend-scroll">
        {candidates.map((c, i) => (
          <div
            key={i}
            className={`rec-card${editing ? " draggable" : ""}${detail === c ? " rec-card-active" : ""}`}
            draggable={editing}
            onDragStart={editing ? (e) => {
              e.dataTransfer.setData("application/json", JSON.stringify(c));
              e.dataTransfer.effectAllowed = "copy";
            } : undefined}
            onClick={() => setDetail(detail === c ? null : c)}
          >
            <div
              className="rec-thumb"
              style={c.photo ? { backgroundImage: `url('${c.photo}')` } : undefined}
            />
            <div className="rec-name">{c.name}</div>
            <div className="rec-meta">
              {c.rating != null && <span><UiIcon name="star" size={12} /> {Number(c.rating).toFixed(1)}</span>}
            </div>
          </div>
        ))}
      </div>
      {detail && (
        <div className="rec-detail-popup">
          <button className="rec-detail-close" onClick={() => setDetail(null)} aria-label="关闭候选景点详情"><UiIcon name="close" size={15} /></button>
          {detail.photo && (
            <div className="rec-detail-photo" style={{ backgroundImage: `url('${detail.photo}')` }} />
          )}
          <div className="rec-detail-name">{detail.name}</div>
          <div className="rec-detail-rows">
            {detail.rating != null && (
              <div className="rec-detail-row">
                <span className="rec-detail-icon"><UiIcon name="star" size={15} /></span>
                <span>{Number(detail.rating).toFixed(1)} 分</span>
              </div>
            )}
            {detail.address && (
              <div className="rec-detail-row">
                <span className="rec-detail-icon"><UiIcon name="location" size={15} /></span>
                <span>{detail.address}</span>
              </div>
            )}
            {detail.open_time && (
              <div className="rec-detail-row">
                <span className="rec-detail-icon"><UiIcon name="clock" size={15} /></span>
                <span>{detail.open_time}</span>
              </div>
            )}
            {formatPoiCost(detail.cost) && (
              <div className="rec-detail-row">
                <span className="rec-detail-icon"><UiIcon name="wallet" size={15} /></span>
                <span>{formatPoiCost(detail.cost)}</span>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

/* ── 周边搜索弹层 ────────────────────────────────── */
function NearbySearchModal({ location, name, onClose, onPickMeal }) {
  const [tab, setTab] = React.useState("attraction");
  const [radius, setRadius] = React.useState(1500);
  const [results, setResults] = React.useState(null);
  const [loading, setLoading] = React.useState(false);
  const [err, setErr] = React.useState("");

  const doSearch = async (t, r) => {
    setLoading(true); setErr(""); setResults(null);
    try {
      const res = await searchNearby(location.lat, location.lng, t === "attraction" ? "风景名胜" : "餐饮服务", r);
      setResults(res);
      if (!res.length) setErr("附近暂无数据，试试调大半径");
    } catch (e) { setErr(e.message || "搜索失败"); }
    finally { setLoading(false); }
  };

  React.useEffect(() => { doSearch(tab, radius); }, []); // eslint-disable-line

  const switchTab = (t) => { setTab(t); doSearch(t, radius); };
  const changeRadius = (r) => { setRadius(r); doSearch(tab, r); };

  return (
    <div className="modal-backdrop" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal-card nearby-modal">
        <button className="modal-close" onClick={onClose} aria-label="关闭周边搜索"><UiIcon name="close" size={18} /></button>
        <div className="modal-title"><UiIcon name="location" size={19} />{name} 周边</div>
        <div className="nearby-tabs">
          <button className={`nearby-tab${tab === "attraction" ? " active" : ""}`} onClick={() => switchTab("attraction")}>景点</button>
          <button className={`nearby-tab${tab === "restaurant" ? " active" : ""}`} onClick={() => switchTab("restaurant")}>餐厅</button>
        </div>
        <div className="nearby-radius-row">
          {[500, 1000, 1500, 3000].map(r => (
            <button key={r} className={`radius-btn${radius === r ? " active" : ""}`} onClick={() => changeRadius(r)}>{r}m</button>
          ))}
        </div>
        {loading && <div className="poi-search-err">搜索中…</div>}
        {err && <div className="poi-search-err">{err}</div>}
        <div className="nearby-results">
          {(results || []).map((p, i) => (
            <div key={i} className="nearby-result">
              <div className="nearby-result-name">{p.name}</div>
              <div className="nearby-result-meta">
                {p.rating != null && <span><UiIcon name="star" size={12} /> {Number(p.rating).toFixed(1)}</span>}
                {p.distance != null && <span> · {p.distance}m</span>}
                {p.address && <span> · {p.address}</span>}
              </div>
              {tab === "restaurant" && onPickMeal && (
                <div className="nearby-meal-actions">
                  <button className="nearby-meal-btn" onClick={() => onPickMeal(p, "lunch")}>设为午餐</button>
                  <button className="nearby-meal-btn" onClick={() => onPickMeal(p, "dinner")}>设为晚餐</button>
                </div>
              )}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

/* ── Sweep 评测侧栏 ───────────────────────────────── */
const GRADERS = [
  { key: "g1_closed_pool",           label: "G1 无幻觉" },
  { key: "g2_time_check",            label: "G2 时间核查" },
  { key: "g3_proximity",             label: "G3 地理跨度" },
  { key: "g4_structure",             label: "G4 结构合法" },
  { key: "g5_coverage",              label: "G5 覆盖完整" },
  { key: "g6_weather",               label: "G6 天气合规" },
  { key: "g7_convergence",           label: "G7 收敛" },
  { key: "g8_time_check_efficiency", label: "G8 TC效率" },
];

function SweepEvalPanel({ code, reviewRounds, timeCheckRounds, profileUpdate, dialogue, overallPass, elapsedS }) {
  const results = code?.results || {};
  return (
    <div className="sweep-eval-panel">
      <div className={`sweep-verdict ${overallPass ? "pass" : "fail"}`}>
        <UiIcon name={overallPass ? "success" : "failure"} size={16} />{overallPass ? "通过" : "未通过"}
        {elapsedS != null && <span className="sweep-elapsed">{elapsedS}s</span>}
      </div>

      <div className="sweep-section-title">代码打分</div>
      {GRADERS.map(({ key, label }) => {
        const r = results[key];
        if (!r) return null;
        const isNA = !r.passed && r.detail?.includes("跳过");
        return (
          <React.Fragment key={key}>
            <div className={`sweep-g-row ${isNA ? "na" : r.passed ? "pass" : "fail"}`}>
              <span>{isNA ? "—" : <UiIcon name={r.passed ? "success" : "failure"} size={14} />}</span>
              <span>{label}</span>
            </div>
            {!r.passed && !isNA && <div className="sweep-g-detail">{r.detail}</div>}
          </React.Fragment>
        );
      })}

      <div className="sweep-section-title">流程</div>
      <div className="sweep-stat">评审：{reviewRounds} 轮</div>
      <div className="sweep-stat">time_check：{timeCheckRounds} 轮</div>

      {profileUpdate && !profileUpdate.error && (
        <>
          <div className="sweep-section-title">画像更新</div>
          {(profileUpdate.diff || []).length > 0
            ? profileUpdate.diff.map((d, i) => <div key={i} className="sweep-profile-row">{d}</div>)
            : <div className="sweep-profile-row sweep-na">无变更</div>}
          {(profileUpdate.change_log || []).length > 0 && (
            <details className="sweep-details">
              <summary>变更理由</summary>
              {profileUpdate.change_log.map((c, i) => <p key={i}>{c}</p>)}
            </details>
          )}
        </>
      )}

      {(dialogue || []).length > 0 && (
        <details className="sweep-details">
          <summary><UiIcon name="archive" size={14} />规划对话</summary>
          {dialogue.map((d, i) => <p key={i}>{d}</p>)}
        </details>
      )}
    </div>
  );
}

Object.assign(window, {
  UiIcon, WeatherGlyph,
  DayMap, MapPanel, Timeline, NavRow, Thumb,
  RecommendStrip, NearbySearchModal,
  SweepEvalPanel, walkNote, WalkIcon,
});

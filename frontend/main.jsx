// main.jsx — App 壳：导航 / 主题 / 认证 / 路由

const TWEAK_DEFAULTS = /*EDITMODE-BEGIN*/{
  "theme": "morning",
  "mascot": true
}/*EDITMODE-END*/;

const THEME_OPTIONS = [
  { value: "morning", label: "青湾手账 · 亮色" },
  { value: "night", label: "青湾夜航 · 暗色" },
];

function App() {
  const [appearance, setTweak] = useTweaks(TWEAK_DEFAULTS);
  const {mode:themeMode, theme, chooseMode:chooseThemeMode} = ThemeState.usePreference();
  const t = {...appearance, theme};
  const legacyHistoryPath = window.location.pathname === "/history";
  const initialPathPage = window.location.pathname === "/profile" ? "profile" : "home";
  const [page, setPage] = React.useState(initialPathPage);
  const [authUser, setAuthUser] = React.useState(() => getAuth()?.username || null);
  const [showAuthModal, setShowAuthModal] = React.useState(false);
  const [authReason, setAuthReason] = React.useState("");
  const [pendingAuthAction, setPendingAuthAction] = React.useState(null);
  const [showUserMenu, setShowUserMenu] = React.useState(false);
  // 行程详情页数据
  const [detailPlan, setDetailPlan] = React.useState(null);
  const [detailPlanId, setDetailPlanId] = React.useState(null);
  const [revisionTrigger, setRevisionTrigger] = React.useState(null);
  const [homePrompt, setHomePrompt] = React.useState("");
  const [workspaceTransition, setWorkspaceTransition] = React.useState("idle");
  const revisionNonceRef = React.useRef(0);
  const workspaceTransitionTimersRef = React.useRef([]);

  const authScopeRef = React.useRef({ username: authUser });
  const pageRef = React.useRef(page);
  pageRef.current = page;
  const planOpenRequestRef = React.useRef(0);
  const [planOpenError, setPlanOpenError] = React.useState("");
  const setCurrentUser = username => {
    authScopeRef.current = { username };
    setAuthUser(username);
    setDetailPlan(null);
    setDetailPlanId(null);
    setPlanOpenError("");
  };

  React.useEffect(() => () => {
    workspaceTransitionTimersRef.current.forEach(window.clearTimeout);
  }, []);

  React.useEffect(() => {
    requestAnimationFrame(() => requestAnimationFrame(() => {
      document.documentElement.classList.add("anim-ready");
    }));
    // 处理 URL 参数
    const params = new URLSearchParams(window.location.search);
    if (initialPathPage === "profile" && !getAuth()) {
      setAuthReason("请先登录管理旅行画像");
      setShowAuthModal(true);
    }
    if (legacyHistoryPath) history.replaceState({}, "", "/");
    if (params.get("login") === "1" && !getAuth()) {
      setPendingAuthAction(NavigationState.chatTarget(false));
      setShowAuthModal(true);
      history.replaceState({}, "", "/");
    }
    const viewId = params.get("view_plan_id");
    if (viewId) {
      history.replaceState({}, "", "/");
      const a = getAuth();
      if (!a) {
        setPendingAuthAction(NavigationState.detailTarget(viewId));
        setAuthReason("登录后继续打开这份行程");
        setShowAuthModal(true);
        return;
      }
      openSavedPlan(viewId);
    }
  }, []);

  // 页面加载时验证 token 有效性
  React.useEffect(() => {
    const scope = authScopeRef.current;
    checkAuth().then(result => {
      if (!result && authScopeRef.current === scope) setCurrentUser(null);
    }).catch(() => {
      // Preserve the current session on server/network errors; 401 expires it.
    });
    const onExpired = () => { setCurrentUser(null); go("home"); };
    window.addEventListener("auth:expired", onExpired);
    return () => window.removeEventListener("auth:expired", onExpired);
  }, []);

  React.useEffect(() => {
    document.documentElement.setAttribute("data-theme", t.theme || "morning");
  }, [t.theme]);

  React.useEffect(() => {
    document.documentElement.setAttribute("data-mascot", t.mascot ? "on" : "off");
  }, [t.mascot]);

  // 点击空白关闭用户菜单（mousedown 避免与按钮 click 冲突）
  React.useEffect(() => {
    if (!showUserMenu) return;
    const handler = (e) => {
      if (!e.target.closest(".user-chip")) {
        setShowUserMenu(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [showUserMenu]);

  const go = (p) => {
    setPage(p);
    window.scrollTo({ top: 0 });
  };

  const beginRevision = (query, planId) => {
    setRevisionTrigger({
      itineraryId: planId,
      content: query.trim(),
      nonce: ++revisionNonceRef.current,
    });
    setPage("chat");
    window.scrollTo({ top: 0 });
  };

  const beginFromHome = (prompt) => {
    setHomePrompt(prompt || "");
    if (!authUser) {
      requestLogin("登录后开始规划你的旅行", NavigationState.chatTarget(prompt || ""));
      return;
    }
    if (workspaceTransition !== "idle") return;
    setWorkspaceTransition("home-out");
    const leaveTimer = window.setTimeout(() => {
      setPage("chat");
      window.scrollTo({ top: 0 });
      setWorkspaceTransition("chat-in");
      const settleTimer = window.setTimeout(() => setWorkspaceTransition("idle"), 520);
      workspaceTransitionTimersRef.current.push(settleTimer);
    }, 210);
    workspaceTransitionTimersRef.current.push(leaveTimer);
  };

  const onRequestModify = (query, planId) => {
    const target = NavigationState.revisionTarget(planId, query);
    if (!target) return;
    if (!authUser) {
      requestLogin("登录后继续修改这份行程", target);
      return;
    }
    beginRevision(target.content, target.itineraryId);
  };

  const requestLogin = (reason = "请先登录再继续", continuation = null) => {
    setAuthReason(reason);
    setPendingAuthAction(continuation);
    setShowAuthModal(true);
  };

  const onAuthSuccess = (username) => {
    setCurrentUser(username);
    setShowAuthModal(false);
    setAuthReason("");
    const continuation = NavigationState.resolveAfterAuth(pendingAuthAction);
    setPendingAuthAction(null);
    if (!continuation) return;
    if (continuation.page === "profile") {
      go("profile");
      return;
    }
    if (continuation.page === "detail" && continuation.planId) {
      openSavedPlan(continuation.planId);
      return;
    }
    if (continuation.mode === "revision") {
      beginRevision(continuation.content, continuation.itineraryId);
      return;
    }
    if (continuation.page === "chat") {
      setHomePrompt(continuation.draft || "");
      setPage("chat");
      window.scrollTo({ top: 0 });
    }
  };

  const openChat = () => {
    if (!authUser) {
      requestLogin(
        "登录后继续你的旅行对话",
        NavigationState.chatTarget(),
      );
      return;
    }
    setPage("chat");
    window.scrollTo({ top: 0 });
  };

  const logout = () => {
    clearAuth();
    setCurrentUser(null);
    setShowUserMenu(false);
    go("home");
  };

  const onOpenHistoryPlan = (rawPlan, planId) => {
    const adapted = adaptPlan(rawPlan, authScopeRef.current.username);
    setDetailPlan(adapted);
    setDetailPlanId(planId);
    setPage("detail");
    window.scrollTo({ top: 0 });
  };


  const openSavedPlan = async planId => {
    const sourcePage = pageRef.current;
    const scope = authScopeRef.current;
    const request = ++planOpenRequestRef.current;
    if (!scope.username) return;
    setPlanOpenError("");
    try {
      const data = await getHistoryItem(planId);
      if (authScopeRef.current !== scope || pageRef.current !== sourcePage || request !== planOpenRequestRef.current) return;
      if (!data?.plan) throw new Error("这份行程暂时无法打开，请重试");
      onOpenHistoryPlan(data.plan, planId);
    } catch (error) {
      if (authScopeRef.current === scope && pageRef.current === sourcePage && request === planOpenRequestRef.current) {
        setPlanOpenError(error.message || "这份行程暂时无法打开，请重试");
      }
    }
  };

  const initial = authUser ? authUser.slice(-1) : "";

  return (
    <div className={`app-shell page-${page} workspace-transition-${workspaceTransition}`}>
      {page !== "detail" && <header className="topbar">
        <div className="brand" onClick={() => go("home")}>
          <div className="brand-glyph"><img src={t.theme === "night" ? "/waygent-dark.svg?v=20261003" : "/waygent.svg?v=20261003"} alt="" /></div>
          <div>
            <div className="brand-name">WAygent · AI 旅行规划</div>
            <div className="brand-sub">Travel Journal by Agents</div>
          </div>
        </div>
        <nav className="topnav">
          <button className={`topnav-link ${page === "home" ? "active" : ""}`}
            onClick={() => go("home")}>
            首页
          </button>
          <button className={`topnav-link ${page === "chat" ? "active" : ""}`}
            onClick={openChat}>
            旅行工作区
          </button>
          <button className={`topnav-link ${page === "profile" ? "active" : ""}`}
            onClick={() => { if (!authUser) { requestLogin("请先登录管理旅行画像", NavigationState.profileTarget()); return; } go("profile"); }}>
            我的画像
          </button>
        </nav>

        <div className="theme-switcher" role="group" aria-label="切换主题">
          <button className="theme-seg" title={t.theme === "night" ? "切换到青湾手账 · 亮色" : "切换到青湾夜航 · 暗色"}
            aria-label={t.theme === "night" ? "切换到亮色" : "切换到暗色"}
            onClick={() => chooseThemeMode(t.theme === "night" ? "morning" : "night")}>
            <UiIcon name={t.theme === "night" ? "sun" : "moon"} size={15} />
          </button>
          <button className={`theme-seg${themeMode === "system" ? " active" : ""}`} title="跟随系统"
            aria-label="跟随系统" aria-pressed={themeMode === "system"} onClick={() => chooseThemeMode("system")}>
            <UiIcon name="sparkle" size={15} />
          </button>
        </div>

        {authUser ? (
          <div className="user-chip" onClick={(e) => { e.stopPropagation(); setShowUserMenu(v => !v); }}>
            <span className="chip-name">{authUser}</span>
            <span className="avatar-dot">{initial}</span>
            {showUserMenu && (
              <div className="user-dropdown" onClick={e => e.stopPropagation()}>
                <button onClick={logout}>退出登录</button>
              </div>
            )}
          </div>
        ) : (
          <button className="user-login-btn" onClick={() => requestLogin("")}>登录 / 注册</button>
        )}
      </header>}

      {showAuthModal && (
        <AuthModal
          reason={authReason}
          onSuccess={onAuthSuccess}
          onClose={() => { setShowAuthModal(false); setAuthReason(""); setPendingAuthAction(null); }}
        />
      )}

      {page === "detail" && detailPlan && (
        <TripDetailPage
          plan={detailPlan}
          planId={detailPlanId}
          onRequestModify={onRequestModify}
          currentUsername={authUser}
          onBack={() => go("chat")}
          onPlanChange={(rawPlan, nextPlanId) => {
            setDetailPlan(adaptPlan(rawPlan, authUser));
            setDetailPlanId(nextPlanId);
          }}
        />
      )}
      {page === "home" && (
        <DashboardPage
          currentUsername={authUser}
          onStart={beginFromHome}
          onDraftChange={setHomePrompt}
          onOpenWorkspace={openChat}
          onOpenPlan={openSavedPlan}
          planOpenError={planOpenError}
        />
      )}
      {page === "chat" && (
        <ChatPage
          currentUsername={authUser}
          onRequestLogin={() => requestLogin("登录后继续你的旅行对话", NavigationState.chatTarget())}
          revisionTrigger={revisionTrigger}
          initialDraft={homePrompt}
          onInitialDraftConsumed={() => setHomePrompt("")}
          onRevisionConsumed={(nonce) => {
            setRevisionTrigger(current => current?.nonce === nonce ? null : current);
          }}
          onOpenPlan={openSavedPlan}
        />
      )}
      {page === "profile" && (
        <ProfilePage currentUsername={authUser} />
      )}

      {page !== "detail" && <TweaksPanel>
        <TweakSection label="整体方案" />
        <TweakSelect
          label="主题"
          value={themeMode}
          options={[...THEME_OPTIONS, {value:"system", label:"跟随系统"}]}
          onChange={chooseThemeMode}
        />
        <TweakSection label="虚拟形象" />
        <TweakToggle label="显示向导「小way」" value={t.mascot} onChange={(v) => setTweak("mascot", v)} />
      </TweaksPanel>}
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);

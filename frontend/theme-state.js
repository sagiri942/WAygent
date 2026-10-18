// Theme preferences are independent of account, navigation and planning state.
const ThemeState = (() => {
  const storageKey = "waygent.theme-mode";
  const validMode = mode => ["system", "morning", "night"].includes(mode);
  function readMode() {
    try {
      const stored = localStorage.getItem(storageKey);
      return validMode(stored) ? stored : "system";
    } catch {
      return "system";
    }
  }
  const mediaQuery = () => window.matchMedia("(prefers-color-scheme: dark)");
  const resolve = (mode, dark) => mode === "system" ? (dark ? "night" : "morning") : mode;

  function usePreference() {
    const [mode, setMode] = React.useState(readMode);
    const [dark, setDark] = React.useState(() => mediaQuery().matches);
    React.useEffect(() => {
      const media = mediaQuery();
      const update = event => setDark(event.matches);
      setDark(media.matches);
      media.addEventListener("change", update);
      return () => media.removeEventListener("change", update);
    }, []);
    const chooseMode = next => {
      if (!validMode(next)) return;
      setMode(next);
      // Private browsing or blocked storage still permits an in-session choice.
      try { localStorage.setItem(storageKey, next); } catch {}
    };
    return {mode, theme:resolve(mode, dark), chooseMode};
  }

  function initialize() {
    document.documentElement.setAttribute("data-theme", resolve(readMode(), mediaQuery().matches));
  }
  return {usePreference, initialize};
})();
ThemeState.initialize();

// mascot.jsx — 背包机器人「小way」
// 用法保持不变: <Mascot size={160} pose="wave" />
// pose: idle | wave | walk | point | cheer | think

function Mascot({ size = 140, pose = "idle", flip = false, style = {} }) {
  const showMap = !["wave", "cheer", "walk"].includes(pose);
  return (
    <div
      className={`mascot mascot-${pose}`}
      style={{ width: size, height: size * 1.18, flexShrink: 0, ...style, transform: flip ? "scaleX(-1)" : undefined }}
      aria-label="背包机器人小way"
      role="img"
    >
      <style>{`
        .mascot svg { width: 100%; height: 100%; display: block; overflow: visible; }
        .mascot .m-body-grp { transform-origin: 60px 120px; }
        .mascot .m-arm-r { transform-origin: 81px 89px; }
        .mascot .m-arm-l { transform-origin: 39px 89px; }
        .mascot .m-leg-l { transform-origin: 48px 121px; }
        .mascot .m-leg-r { transform-origin: 72px 121px; }
        .mascot .m-eyes { transform-origin: 60px 53px; }
        .mascot .m-flag { transform-origin: 87px 76px; }
        .mascot .m-shadow { transform-origin: 60px 142px; }
        @keyframes m-blink { 0%, 94%, 100% { transform: scaleY(1); } 96.5% { transform: scaleY(.12); } }

        @media (prefers-reduced-motion: no-preference) {
          .mascot .m-eyes { animation: m-blink 4.2s infinite; }
          .mascot-idle .m-body-grp, .mascot-wave .m-body-grp, .mascot-point .m-body-grp, .mascot-think .m-body-grp { animation: m-bob 3.2s ease-in-out infinite; }
          @keyframes m-bob { 0%,100% { transform: translateY(0); } 50% { transform: translateY(-3px); } }
          .mascot-wave .m-arm-r { animation: m-wave 1.6s ease-in-out infinite; }
          @keyframes m-wave { 0%,100% { transform: rotate(-64deg); } 50% { transform: rotate(-28deg); } }
          .mascot-walk .m-body-grp { animation: m-trot .55s ease-in-out infinite; }
          @keyframes m-trot { 0%,100% { transform: translateY(0) rotate(1.2deg); } 50% { transform: translateY(-4px) rotate(-1.2deg); } }
          .mascot-walk .m-leg-l { animation: m-step .55s ease-in-out infinite; }
          .mascot-walk .m-leg-r { animation: m-step .55s ease-in-out infinite reverse; }
          @keyframes m-step { 0%,100% { transform: rotate(20deg); } 50% { transform: rotate(-20deg); } }
          .mascot-walk .m-arm-l { animation: m-swing .55s ease-in-out infinite; }
          @keyframes m-swing { 0%,100% { transform: rotate(-14deg); } 50% { transform: rotate(14deg); } }
          .mascot-cheer .m-arm-r { animation: m-cheer-r 1s ease-in-out infinite; }
          .mascot-cheer .m-arm-l { animation: m-cheer-l 1s ease-in-out infinite; }
          @keyframes m-cheer-r { 0%,100% { transform: rotate(-50deg); } 50% { transform: rotate(-69deg); } }
          @keyframes m-cheer-l { 0%,100% { transform: rotate(50deg); } 50% { transform: rotate(69deg); } }
          .mascot-cheer .m-body-grp { animation: m-hop .9s ease-in-out infinite; }
          @keyframes m-hop { 0%,100% { transform: translateY(0); } 40% { transform: translateY(-7px); } }
          .mascot-think .m-flag { animation: m-tilt 2.6s ease-in-out infinite; }
          @keyframes m-tilt { 0%,100% { transform: rotate(-4deg); } 50% { transform: rotate(5deg); } }
        }
      `}</style>
      <svg viewBox="0 0 120 150" aria-hidden="true">
        <ellipse className="m-shadow" cx="60" cy="142" rx="29" ry="5" fill="var(--ink)" opacity=".12" />
        <g className="m-body-grp" stroke="var(--ink)" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
          <rect x="79" y="75" width="24" height="49" rx="10" fill="var(--mascot-coat)" />
          <path d="M85 82h11v15H85m0 10h11" fill="none" />
          <g className="m-leg-l"><path d="M47 119v12"/><rect x="34" y="130" width="23" height="10" rx="5" fill="var(--mascot-hat)" /></g>
          <g className="m-leg-r"><path d="M72 119v12"/><rect x="64" y="130" width="23" height="10" rx="5" fill="var(--mascot-hat)" /></g>
          <rect x="33" y="75" width="54" height="52" rx="18" fill="var(--mascot-skin)" />
          <path d="M41 78v36m37-36v36" stroke="var(--mascot-coat)" strokeWidth="5" />
          <rect x="46" y="91" width="27" height="22" rx="7" fill="var(--card)" />
          <circle cx="60" cy="102" r="4" fill="var(--mascot-hat)" />
          <path d="M60 22V12" /><circle cx="60" cy="9" r="5" fill="var(--mascot-coat)" />
          <rect x="23" y="23" width="74" height="56" rx="21" fill="var(--mascot-hat)" />
          <rect x="31" y="32" width="58" height="36" rx="14" fill="var(--card)" />
          <g className="m-eyes" stroke="var(--ink)" strokeWidth="4">
            {pose === "cheer" ? <><path d="m41 48 5-4 5 4"/><path d="m69 48 5-4 5 4"/></> : <><path d="M45 44v7"/><path d="M75 44v7"/></>}
            <path d="M53 57q7 6 14 0" strokeWidth="2.5" fill="none" />
          </g>
          <g className="m-arm-l"><path d="M34 87q-11 5-9 20" fill="none" strokeWidth="10" stroke="var(--mascot-skin)" /><circle cx="26" cy="111" r="7" fill="var(--mascot-hat)" /></g>
          <g className="m-arm-r" style={pose === "point" ? { transform: "rotate(-35deg)" } : undefined}><path d="M86 87q11 5 9 20" fill="none" strokeWidth="10" stroke="var(--mascot-skin)" /><circle cx="94" cy="111" r="7" fill="var(--mascot-hat)" /></g>
          {pose === "point" && <g className="m-flag"><path d="M100 104V76" fill="none"/><path d="M100 76h14l-4 7 4 7h-14Z" fill="var(--mascot-coat)"/></g>}
          {showMap && <g><path d="m39 107 14-4 10 4 15-5v23l-15 5-10-4-14 4Z" fill="var(--card)"/><path d="m53 104 1 20m9-16v21" stroke="var(--mascot-coat)"/></g>}
          {pose === "think" && <g className="m-flag"><path d="M100 35q0-6 6-6t6 6q0 4-6 7v3" fill="none"/><circle cx="106" cy="51" r="1" /></g>}
        </g>
      </svg>
    </div>
  );
}

Object.assign(window, { Mascot });

export function NeoLogo() {
  return (
    <svg
      className="neo-logo"
      viewBox="0 0 72 44"
      role="img"
      aria-label="Neo Harness"
    >
      <defs>
        <linearGradient
          id="neo-logo-green"
          x1="10"
          y1="50"
          x2="42"
          y2="60"
          gradientUnits="userSpaceOnUse"
        >
          <stop offset="0" stopColor="#a8f7a6" />
          <stop offset="0.52" stopColor="#71d88a" />
          <stop offset="1" stopColor="#2fa86b" />
        </linearGradient>
        <linearGradient
          id="neo-logo-cyan"
          x1="23"
          y1="50"
          x2="20"
          y2="12"
          gradientUnits="userSpaceOnUse"
        >
          <stop offset="0" stopColor="#a3f0ff" />
          <stop offset="0.60" stopColor="#6fc7d8" />
          <stop offset="1" stopColor="#377ca0" />
        </linearGradient>
      </defs>
      <rect className="neo-logo-frame" x="2" y="2" width="68" height="40" />
      <g className="neo-logo-monogram">
        <path className="neo-logo-n-left" d="M18 35V14l6-8v13z" />
        <path className="neo-logo-n-slash" d="M20 12h3L50 33h-10z" />
        <path className="neo-logo-shared" d="M35 35V10l5-1v2z" />
        <path className="neo-logo-h-bar" d="M40 17H55L70 22H48Z" />
        <path className="neo-logo-h-end" d="M53 15L59 10L63 35H59Z" />
      </g>
    </svg>
  );
}

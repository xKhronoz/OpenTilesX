import React, { useEffect, useMemo, useState } from "react";

export default function RefreshMeter({
  label,
  detail,
  loading = false,
  active = false,
  className = "",
  cycleMs = 2400,
  lastUpdatedAt = null,
  activeLabel = "Auto-Refresh Scheduled",
  idleLabel = "Idle",
}) {
  const [now, setNow] = useState(Date.now());

  const shouldShowCountdown = active && !loading && Number.isFinite(lastUpdatedAt) && cycleMs > 0;
  const nextRefreshAt = shouldShowCountdown ? lastUpdatedAt + cycleMs : null;

  useEffect(() => {
    if (!shouldShowCountdown && !loading) return undefined;
    const timer = window.setInterval(() => {
      setNow(Date.now());
    }, shouldShowCountdown ? 250 : 1000);
    return () => window.clearInterval(timer);
  }, [loading, shouldShowCountdown]);

  const progressPercent = useMemo(() => {
    if (!shouldShowCountdown || !nextRefreshAt) return 0;
    const remainingMs = Math.max(0, nextRefreshAt - now);
    if (remainingMs <= 120) return 100;
    const progressed = 1 - remainingMs / cycleMs;
    return Math.max(0, Math.min(100, progressed * 100));
  }, [cycleMs, nextRefreshAt, now, shouldShowCountdown]);

  const statusText = useMemo(() => {
    if (loading) return "Refreshing";
    if (active && shouldShowCountdown && nextRefreshAt) {
      const remainingMs = Math.max(0, nextRefreshAt - now);
      const remainingSeconds = Math.ceil(remainingMs / 1000);
      if (remainingSeconds <= 0) return "Refreshing Soon";
      return `Next Refresh In ${remainingSeconds}s`;
    }
    if (active) return activeLabel;
    return idleLabel;
  }, [active, activeLabel, idleLabel, loading, nextRefreshAt, now, shouldShowCountdown]);

  return (
    <section className={`refresh-meter ${className}`.trim()}>
      <div className="refresh-meter__row">
        <div>
          <p className="refresh-meter__label">{label}</p>
          {detail ? <p className="refresh-meter__detail">{detail}</p> : null}
        </div>
        <span className={`status ${loading ? "loading" : active ? "running" : "unknown"}`} aria-live="polite">
          {statusText}
        </span>
      </div>
      <div className="refresh-meter__track" aria-hidden="true">
        <span
          className={`refresh-meter__fill ${loading ? "is-loading" : active ? "is-active" : ""}`.trim()}
          style={{
            "--refresh-duration": `${cycleMs}ms`,
            "--refresh-progress": `${progressPercent}%`,
          }}
        />
      </div>
    </section>
  );
}
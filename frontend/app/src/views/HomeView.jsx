import React, { useCallback, useEffect, useState } from "react";
import RefreshMeter from "../components/RefreshMeter.jsx";
import openTilesLogo from "../../assets/images/OpenTilesX.svg";
import { appPath } from "../utils/routes.js";

function pretty(value) {
  return typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

export default function HomeView() {
  const [health, setHealth] = useState(null);
  const [error, setError] = useState("");
  const [refreshing, setRefreshing] = useState(true);
  const [lastUpdatedAt, setLastUpdatedAt] = useState(null);

  const loadHealth = useCallback(async () => {
    setRefreshing(true);
    try {
      const response = await fetch("/healthz");
      const contentType = response.headers.get("content-type") || "";
      const body = contentType.includes("application/json") ? await response.json() : await response.text();
      if (!response.ok) {
        throw new Error(typeof body === "string" ? body : `Health request failed (${response.status})`);
      }
      if (typeof body === "string") {
        setHealth({ message: body });
        setError("Health endpoint returned a non-JSON response.");
      } else {
        setHealth(body);
        setError("");
      }
      setLastUpdatedAt(Date.now());
    } catch (fetchError) {
      setError(String(fetchError));
    } finally {
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    loadHealth();
    const interval = window.setInterval(() => {
      if (!document.hidden) loadHealth();
    }, 30000);
    return () => {
      window.clearInterval(interval);
    };
  }, [loadHealth]);

  const adminDisabledMessage = "Enable ADMIN_ENABLED=true to unlock the control plane.";

  return (
    <div className="home-shell">
      <header className="home-header">
        <a className="home-brand brand-link" href={appPath("/")}>
          <img src={openTilesLogo} alt="OpenTilesX" />
          <div>
            <h1>OpenTilesX</h1>
            <p className="eyebrow">Offline-ready tile platform</p>
          </div>
        </a>
        <nav className="home-actions">
          <a href={appPath("/map")}>Open Map</a>
          {health?.admin_enabled ? (
            <a href={appPath("/admin")}>Open Admin</a>
          ) : (
            <span className="home-disabled action-disabled" title={adminDisabledMessage} aria-label={adminDisabledMessage}>
              Open Admin
            </span>
          )}
        </nav>
      </header>

      <main className="home-main">
        <section className="home-hero">
          <div className="home-copy">
            <h2>Cloud-native tile rendering with an offline control plane.</h2>
            <p>
              Serve cached PNG tiles, run render workers against external PostGIS, and keep map bundles portable across
              connected and air-gapped environments.
            </p>
            <div className="button-row">
              <a className="home-button" href={appPath("/map")}>
                Try Map Preview
              </a>
              {health?.admin_enabled ? (
                <a className="home-button secondary-link" href={appPath("/admin")}>
                  Open Admin UI
                </a>
              ) : (
                <span
                  className="home-button secondary-link is-disabled"
                  title={adminDisabledMessage}
                  aria-label={adminDisabledMessage}
                >
                  Open Admin UI
                </span>
              )}
              <button className="secondary" onClick={loadHealth}>
                Refresh Health
              </button>
            </div>
            <div className="home-refresh-shell">
              <RefreshMeter
                label="Runtime Health"
                detail={lastUpdatedAt ? `Last Checked ${new Date(lastUpdatedAt).toLocaleTimeString()}` : "Polling Every 30 Seconds"}
                loading={refreshing}
                active={!refreshing}
                cycleMs={30000}
                lastUpdatedAt={lastUpdatedAt}
              />
            </div>
          </div>
          <div className="home-logo-panel panel">
            <img src={openTilesLogo} alt="" />
            <p>External PostGIS is the authoritative data plane. Tile storage can be filesystem or S3-compatible object storage.</p>
          </div>
        </section>

        <section className="cards home-cards">
          <article className="card">
            <h3>Runtime</h3>
            <pre>{error ? error : pretty(health || "Loading Health...")}</pre>
          </article>
          <article className="card">
            <h3>Routes</h3>
            <dl className="facts">
              <div>
                <dt>`/`</dt>
                <dd>Home and Runtime Overview</dd>
              </div>
              <div>
                <dt>`/map`</dt>
                <dd>Interactive Tile Viewer</dd>
              </div>
              <div>
                <dt>`/admin`</dt>
                <dd>{health?.admin_enabled ? "Admin SPA enabled" : "Disabled until ADMIN_ENABLED=true"}</dd>
              </div>
            </dl>
          </article>
        </section>
      </main>
    </div>
  );
}

import React, { useEffect, useState } from "react";

function pretty(value) {
  return typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

export default function HomeView() {
  const [health, setHealth] = useState(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const response = await fetch("/healthz");
        const data = await response.json();
        if (!cancelled) setHealth(data);
      } catch (fetchError) {
        if (!cancelled) setError(String(fetchError));
      }
    }
    load();
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div className="home-shell">
      <header className="home-header">
        <div className="home-brand">
          <img src="/static/images/OpenTilesX.svg" alt="OpenTilesX" />
          <div>
            <p className="eyebrow">Offline-ready tile platform</p>
            <h1>OpenTilesX</h1>
          </div>
        </div>
        <nav className="home-actions">
          <a href="/map">Open map</a>
          {health?.admin_enabled ? <a href="/admin">Open admin</a> : <span className="home-disabled">Admin disabled</span>}
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
              <a className="home-button" href="/map">
                Try map preview
              </a>
              {health?.admin_enabled ? (
                <a className="home-button secondary-link" href="/admin">
                  Open admin UI
                </a>
              ) : (
                <span className="home-note">Enable `ADMIN_ENABLED=true` to unlock the control plane.</span>
              )}
            </div>
          </div>
          <div className="home-logo-panel panel">
            <img src="/static/images/OpenTilesX.svg" alt="" />
            <p>External PostGIS is the authoritative data plane. Tile storage can be filesystem or S3-compatible object storage.</p>
          </div>
        </section>

        <section className="cards home-cards">
          <article className="card">
            <h3>Runtime</h3>
            <pre>{error ? error : pretty(health || "Loading health...")}</pre>
          </article>
          <article className="card">
            <h3>Routes</h3>
            <dl className="facts">
              <div>
                <dt>`/`</dt>
                <dd>Home and runtime overview</dd>
              </div>
              <div>
                <dt>`/map`</dt>
                <dd>Interactive tile viewer</dd>
              </div>
              <div>
                <dt>`/admin`</dt>
                <dd>{health?.admin_enabled ? "Admin SPA enabled" : "Disabled until ADMIN_ENABLED=true"}</dd>
              </div>
            </dl>
          </article>
          <article className="card">
            <h3>Brand Assets</h3>
            <div className="home-favicons">
              <img src="/static/images/favicons/favicon-32x32.png" alt="32x32 favicon" />
              <img src="/static/images/favicons/apple-touch-icon.png" alt="Apple touch icon" />
              <img src="/static/images/favicons/android-chrome-192x192.png" alt="Android icon" />
            </div>
            <p className="hint">Favicons and logos are served from `/static/images` so the browser and SPA stay in sync.</p>
          </article>
        </section>
      </main>
    </div>
  );
}

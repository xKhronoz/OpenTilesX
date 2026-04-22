import React, { useEffect, useMemo, useState } from "react";

const descriptions = {
  import:
    "Loads a full PBF into the external PostGIS database using create or replace behavior.",
  reimport:
    "Runs a full replacement import. blue_green imports into a new schema before switching metadata.",
  update:
    "Applies an .osc or .osc.gz change file with osm2pgsql append.",
  expire:
    "Queues specific XYZ tiles for rendering again.",
  "external-data/load":
    "Loads only the style's external data tables into PostGIS without re-running the base import.",
  "map-bundle/validate":
    "Checks that a bundle manifest, style, import assets, overlays, and referenced files exist.",
  "map-bundle/activate":
    "Validates a bundle and records it as the active bundle for new API and renderer workers.",
  "map-bundle/generate":
    "Copies a prepared style directory and optional import files into an offline bundle archive.",
};

const templates = {
  import: {
    pbf_uri: "/data/import/region.osm.pbf",
    source_mode: "local",
    external_data_mode: "auto",
  },
  reimport: {
    pbf_uri: "/data/import/region.osm.pbf",
    source_mode: "local",
    replace_strategy: "blue_green",
    external_data_mode: "auto",
  },
  update: {
    change_uri: "/data/updates/latest.osc.gz",
    expiry_file: "/tmp/tile-server/dirty-tiles.txt",
  },
  expire: { tiles: [{ z: 0, x: 0, y: 0 }], reason: "manual-expire" },
  "external-data/load": {
    style_dir: "/opt/openstreetmap-carto-default",
    external_data_mode: "local",
    external_data_uri: "/data/external-data/osm-carto-cache",
    external_data_source_mode: "local",
  },
  "map-bundle/validate": { map_bundle_uri: "file:///data/bundles/default" },
  "map-bundle/activate": { map_bundle_uri: "file:///data/bundles/default" },
  "map-bundle/generate": {
    style_dir: "/opt/openstreetmap-carto-default",
    output: "/data/bundles/generated/offline-map-1.tar.gz",
    name: "offline-map",
    version: "1",
    source_mode: "local",
    fetch_external_data: false,
  },
};

const endpoints = {
  import: "/admin/jobs/import",
  reimport: "/admin/jobs/reimport",
  update: "/admin/jobs/update",
  expire: "/admin/jobs/expire",
  "external-data/load": "/admin/jobs/external-data/load",
  "map-bundle/validate": "/admin/jobs/map-bundle/validate",
  "map-bundle/activate": "/admin/jobs/map-bundle/activate",
  "map-bundle/generate": "/admin/jobs/map-bundle/generate",
};

const examples = {
  localPbf: {
    title: "Local PBF import",
    kind: "import",
    description: "Import a mounted PBF from /data/import without network access.",
    payload: {
      pbf_uri: "/data/import/region.osm.pbf",
      source_mode: "local",
      external_data_mode: "auto",
    },
  },
  localPbfPoly: {
    title: "Local PBF and poly",
    kind: "import",
    description: "Import a mounted PBF and record the matching polygon file for update workflows.",
    payload: {
      pbf_uri: "/data/import/region.osm.pbf",
      poly_uri: "/data/import/region.poly",
      source_mode: "local",
      external_data_mode: "auto",
    },
  },
  localExternalPreload: {
    title: "Local external-data preload",
    kind: "import",
    description: "Import a local PBF and load vendored Carto external data from a mounted preload.",
    payload: {
      pbf_uri: "/data/import/region.osm.pbf",
      source_mode: "local",
      external_data_mode: "local",
      external_data_uri: "/data/external-data/osm-carto-cache",
      external_data_source_mode: "local",
    },
  },
  internalUrl: {
    title: "Internal URL import",
    kind: "import",
    description: "Fetch PBF or poly from an approved internal mirror when policy allows it.",
    payload: {
      pbf_uri: "https://mirror.internal/osm/region.osm.pbf",
      poly_uri: "https://mirror.internal/osm/region.poly",
      source_mode: "internal",
      external_data_mode: "fetch",
      external_data_source_mode: "internal",
    },
  },
  publicUrl: {
    title: "Public URL import",
    kind: "import",
    description: "Fetch a public PBF only when public downloads are explicitly enabled.",
    payload: {
      pbf_uri: "https://download.geofabrik.de/europe/luxembourg-latest.osm.pbf",
      source_mode: "public",
      external_data_mode: "fetch",
      external_data_source_mode: "public",
    },
  },
  externalDataLoad: {
    title: "External data only",
    kind: "external-data/load",
    description: "Load or refresh Carto water, ice, and Natural Earth tables without reimporting the OSM base data.",
    payload: {
      style_dir: "/opt/openstreetmap-carto-default",
      external_data_mode: "local",
      external_data_uri: "/data/external-data/osm-carto-cache",
      external_data_source_mode: "local",
    },
  },
  generate: {
    title: "Generate bundle",
    kind: "map-bundle/generate",
    description: "Build an offline bundle from a prepared style directory.",
    payload: {
      style_dir: "/opt/openstreetmap-carto-default",
      output: "/data/bundles/generated/offline-map-1.tar.gz",
      name: "offline-map",
      version: "1",
      source_mode: "local",
      fetch_external_data: false,
    },
  },
  fetchBundle: {
    title: "Generate bundle with fetch",
    kind: "map-bundle/generate",
    description: "Build a bundle and fetch external data into it when the selected source policy allows it.",
    payload: {
      style_dir: "/opt/openstreetmap-carto-default",
      output: "/data/bundles/generated/offline-map-1.tar.gz",
      name: "offline-map",
      version: "1",
      source_mode: "public",
      fetch_external_data: true,
    },
  },
};

const tabs = [
  ["dashboard", "Dashboard"],
  ["jobs", "Jobs"],
  ["create", "Create Job"],
  ["bundles", "Bundles"],
  ["diagnostics", "Diagnostics"],
  ["examples", "Examples"],
];

function pretty(value) {
  return typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

function healthClass(status) {
  return status || "unknown";
}

function isRemoteUri(value) {
  return /^(https?:\/\/|s3:\/\/|oci:\/\/)/i.test(String(value || "").trim());
}

function copyText(value) {
  if (!value) return Promise.resolve();
  if (navigator.clipboard?.writeText) return navigator.clipboard.writeText(value);
  const input = document.createElement("textarea");
  input.value = value;
  document.body.appendChild(input);
  input.select();
  document.execCommand("copy");
  input.remove();
  return Promise.resolve();
}

function modeReason(capabilities, sourceMode) {
  const external = capabilities?.external_data?.modes || {};
  if (sourceMode === "internal") return external.fetch_internal?.reason || "";
  if (sourceMode === "public") return external.fetch_public?.reason || "";
  return "";
}

function gateForPayload(kind, payload, capabilities) {
  if (!capabilities) return { blocked: false, reason: "" };
  const actions = capabilities.actions || {};
  const action = actions[kind] || {};
  const externalDataUri = String(payload?.external_data_uri || "").trim();
  if (kind === "import" || kind === "reimport" || kind === "external-data/load") {
    const mode = String(payload?.external_data_mode || capabilities?.external_data?.default_mode || "").toLowerCase();
    const sourceMode = String(payload?.external_data_source_mode || "public").toLowerCase();
    if (mode === "fetch") {
      if (sourceMode === "internal" && !action?.fetch_internal?.allowed) {
        return {
          blocked: true,
          reason:
            action?.fetch_internal?.reason ||
            modeReason(capabilities, "internal") ||
            "Internal external-data fetch is not allowed by the current policy.",
        };
      }
      if (sourceMode === "public" && !action?.fetch_public?.allowed) {
        return {
          blocked: true,
          reason:
            action?.fetch_public?.reason ||
            modeReason(capabilities, "public") ||
            "Public external-data fetch is not allowed by the current policy.",
        };
      }
      if (sourceMode === "local") {
        return {
          blocked: true,
          reason:
            "external_data_mode=fetch cannot use external_data_source_mode=local. Use vendored assets or switch to internal/public.",
        };
      }
    }
    if (mode !== "placeholder" && isRemoteUri(externalDataUri)) {
      if (sourceMode === "internal" && !action?.fetch_internal?.allowed) {
        return {
          blocked: true,
          reason:
            action?.fetch_internal?.reason ||
            "Internal external-data preloads are not allowed by the current policy.",
        };
      }
      if (sourceMode === "public" && !action?.fetch_public?.allowed) {
        return {
          blocked: true,
          reason:
            action?.fetch_public?.reason ||
            "Public external-data preloads are not allowed by the current policy.",
        };
      }
      if (sourceMode === "local") {
        return {
          blocked: true,
          reason:
            "Remote external_data_uri cannot use external_data_source_mode=local. Use internal/public with the matching ALLOW_* policy.",
        };
      }
    }
  }
  if (kind === "map-bundle/generate" && payload?.fetch_external_data) {
    const sourceMode = String(payload?.source_mode || "local").toLowerCase();
    if (sourceMode === "internal" && !action?.fetch_internal?.allowed) {
      return {
        blocked: true,
        reason:
          action?.fetch_internal?.reason ||
          modeReason(capabilities, "internal") ||
          "Internal external-data fetch is not allowed by the current policy.",
      };
    }
    if (sourceMode === "public" && !action?.fetch_public?.allowed) {
      return {
        blocked: true,
        reason:
          action?.fetch_public?.reason ||
          modeReason(capabilities, "public") ||
          "Public external-data fetch is not allowed by the current policy.",
      };
    }
    if (sourceMode === "local") {
      return {
        blocked: true,
        reason:
          action?.fetch_local?.reason ||
          "source_mode=local cannot fetch external data. Use an internal/public source or a vendored preload.",
      };
    }
  }
  if (kind === "map-bundle/generate" && isRemoteUri(externalDataUri)) {
    const sourceMode = String(payload?.source_mode || "local").toLowerCase();
    if (sourceMode === "internal" && !action?.fetch_internal?.allowed) {
      return {
        blocked: true,
        reason:
          action?.fetch_internal?.reason ||
          "Internal bundle preloads are not allowed by the current policy.",
      };
    }
    if (sourceMode === "public" && !action?.fetch_public?.allowed) {
      return {
        blocked: true,
        reason:
          action?.fetch_public?.reason ||
          "Public bundle preloads are not allowed by the current policy.",
      };
    }
    if (sourceMode === "local") {
      return {
        blocked: true,
        reason:
          "Remote external_data_uri cannot use source_mode=local. Use an internal/public source or mount the preload locally.",
      };
    }
  }
  return { blocked: false, reason: "" };
}

function exampleGate(example, capabilities) {
  return gateForPayload(example.kind, example.payload, capabilities);
}

async function api(path, token, options = {}) {
  const headers = {
    ...(options.body instanceof FormData ? {} : { "Content-Type": "application/json" }),
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...(options.headers || {}),
  };
  const response = await fetch(path, { ...options, headers });
  const text = await response.text();
  let data = text;
  try {
    data = JSON.parse(text);
  } catch (_) {}
  if (!response.ok) {
    throw data;
  }
  return data;
}

function PolicyBanner({ capabilities }) {
  if (!capabilities) return null;
  const external = capabilities.external_data;
  return (
    <section className="panel policy-banner">
      <div className="panel-title-row">
        <div>
          <h3>External Data Policy</h3>
          <p>{external.default_mode_message}</p>
        </div>
      </div>
      <div className="policy-grid">
        <div className="policy-item">
          <span>Default mode</span>
          <strong>{external.default_mode}</strong>
        </div>
        <div className="policy-item">
          <span>Internal downloads</span>
          <strong>{external.allow_internal_downloads ? "enabled" : "disabled"}</strong>
        </div>
        <div className="policy-item">
          <span>Public downloads</span>
          <strong>{external.allow_public_downloads ? "enabled" : "disabled"}</strong>
        </div>
      </div>
    </section>
  );
}

function CopyIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path
        d="M9 9.75A2.25 2.25 0 0 1 11.25 7.5h7.5A2.25 2.25 0 0 1 21 9.75v7.5a2.25 2.25 0 0 1-2.25 2.25h-7.5A2.25 2.25 0 0 1 9 17.25z"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
      />
      <path
        d="M15 7.5V6.75A2.25 2.25 0 0 0 12.75 4.5h-7.5A2.25 2.25 0 0 0 3 6.75v7.5a2.25 2.25 0 0 0 2.25 2.25H6"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
      />
    </svg>
  );
}

function RefreshIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path
        d="M20 12a8 8 0 1 1-2.34-5.66"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
      />
      <path
        d="M20 4v6h-6"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function TrashIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d="M4 7h16" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
      <path
        d="M9 7V5.75A1.75 1.75 0 0 1 10.75 4h2.5A1.75 1.75 0 0 1 15 5.75V7"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
      />
      <path
        d="M7 7l.8 11a2 2 0 0 0 2 1.85h4.4a2 2 0 0 0 2-1.85L17 7"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
      />
      <path d="M10 10.5v5M14 10.5v5" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
    </svg>
  );
}

export default function AdminView() {
  const [token, setToken] = useState(localStorage.getItem("tileAdminToken") || "");
  const [draftToken, setDraftToken] = useState(localStorage.getItem("tileAdminToken") || "");
  const [activeTab, setActiveTab] = useState("dashboard");
  const [capabilities, setCapabilities] = useState(null);
  const [health, setHealth] = useState("Not loaded.");
  const [jobs, setJobs] = useState([]);
  const [statusFilter, setStatusFilter] = useState("");
  const [jobLookup, setJobLookup] = useState("");
  const [jobDetails, setJobDetails] = useState("Open a job to inspect payload, result, and errors.");
  const [kind, setKind] = useState("import");
  const [payloadText, setPayloadText] = useState(JSON.stringify(templates.import, null, 2));
  const [createOutput, setCreateOutput] = useState("Ready.");
  const [bundleState, setBundleState] = useState({
    name: "offline-map",
    version: "1",
    style_dir: "/opt/openstreetmap-carto-default",
    output: "/data/bundles/generated/offline-map-1.tar.gz",
    pbf_uri: "",
    poly_uri: "",
    external_data_uri: "",
    source_mode: "local",
    fetch_external_data: false,
  });
  const [bundleValidateUri, setBundleValidateUri] = useState("file:///data/bundles/default");
  const [bundleGenerateOutput, setBundleGenerateOutput] = useState("Ready.");
  const [bundleValidateOutput, setBundleValidateOutput] = useState("Ready.");
  const [bundleUpload, setBundleUpload] = useState(null);
  const [diagnostics, setDiagnostics] = useState(null);
  const [diagnosticsOutput, setDiagnosticsOutput] = useState("Ready.");
  const [diagnosticTile, setDiagnosticTile] = useState({ layer: "default", z: 0, x: 0, y: 0 });
  const [failuresLimit, setFailuresLimit] = useState(20);
  const [exampleOutput, setExampleOutput] = useState("Select Preview on any example card to inspect the payload first.");
  const [toasts, setToasts] = useState([]);

  const parsedPayload = useMemo(() => {
    try {
      return { value: JSON.parse(payloadText || "{}"), error: null };
    } catch (error) {
      return { value: null, error: error.message };
    }
  }, [payloadText]);

  const createGate = useMemo(() => {
    if (!parsedPayload.value) return { blocked: false, reason: "" };
    return gateForPayload(kind, parsedPayload.value, capabilities);
  }, [kind, parsedPayload, capabilities]);

  function addToast(message, type = "ok", detail = "") {
    const id = crypto.randomUUID();
    setToasts((items) => [...items, { id, message, type, detail }]);
    window.setTimeout(() => {
      setToasts((items) => items.filter((item) => item.id !== id));
    }, 7000);
  }

  function dismissToast(id) {
    setToasts((items) => items.filter((item) => item.id !== id));
  }

  async function loadHealth() {
    try {
      const response = await fetch("/healthz");
      const data = await response.json();
      setHealth(pretty(data));
    } catch (error) {
      setHealth(String(error));
    }
  }

  async function loadCapabilities(nextToken = token) {
    if (!nextToken) {
      setCapabilities(null);
      return;
    }
    try {
      const data = await api("/admin/capabilities", nextToken);
      setCapabilities(data);
    } catch (error) {
      setCapabilities(null);
      addToast("Capabilities failed", "error", typeof error === "string" ? error : pretty(error));
    }
  }

  async function loadJobs() {
    if (!token) return;
    try {
      const query =
        "/admin/jobs?limit=50" + (statusFilter ? `&status=${encodeURIComponent(statusFilter)}` : "");
      const data = await api(query, token);
      setJobs(data.jobs || []);
    } catch (error) {
      setJobDetails(pretty(error));
      addToast("Could not load jobs", "error", "Check the admin token and metadata database connection.");
    }
  }

  async function openJob(id) {
    if (!token || !id) return;
    try {
      const data = await api(`/admin/jobs/${encodeURIComponent(id)}`, token);
      setJobDetails(pretty(data));
      addToast("Job loaded", "ok", `${data.kind || "job"} is ${data.status || "unknown"}.`);
    } catch (error) {
      setJobDetails(pretty(error));
      addToast("Job lookup failed", "error", "The job may not exist or the token may be invalid.");
    }
  }

  async function createJob(path, payload, onOutput, kindLabel) {
    try {
      const data = await api(path, token, {
        method: "POST",
        body: JSON.stringify(payload),
      });
      onOutput(pretty(data));
      if (data.id) setJobLookup(data.id);
      addToast("Job created", "ok", `${kindLabel} ${data.id || ""}`.trim());
      await loadJobs();
    } catch (error) {
      onOutput(pretty(error));
      addToast("Job creation failed", "error", "Review the response panel on this page.");
    }
  }

  async function loadDiagnostics() {
    if (!token) return;
    try {
      const data = await api(
        `/admin/diagnostics?failures_limit=${encodeURIComponent(failuresLimit)}`,
        token,
      );
      setDiagnostics(data);
    } catch (error) {
      setDiagnosticsOutput(pretty(error));
      addToast("Diagnostics failed", "error", "Check the admin token and metadata database connection.");
    }
  }

  useEffect(() => {
    loadHealth();
  }, []);

  useEffect(() => {
    loadCapabilities(token);
    loadJobs();
    if (activeTab === "diagnostics") loadDiagnostics();
  }, [token]);

  useEffect(() => {
    if (activeTab !== "diagnostics" || !token) return undefined;
    loadDiagnostics();
    const interval = window.setInterval(() => {
      if (!document.hidden) loadDiagnostics();
    }, 10000);
    return () => window.clearInterval(interval);
  }, [activeTab, token, failuresLimit]);

  useEffect(() => {
    if (token) loadJobs();
  }, [statusFilter]);

  function saveToken() {
    localStorage.setItem("tileAdminToken", draftToken);
    setToken(draftToken);
    addToast("Token saved", "ok", "Admin API requests will use it from this browser.");
  }

  function selectTemplate(nextKind) {
    setKind(nextKind);
    setPayloadText(JSON.stringify(templates[nextKind] || {}, null, 2));
  }

  function formatPayload() {
    if (!parsedPayload.value) {
      setCreateOutput(`Invalid JSON: ${parsedPayload.error}`);
      addToast("Invalid JSON", "error", parsedPayload.error);
      return;
    }
    setPayloadText(JSON.stringify(parsedPayload.value, null, 2));
    addToast("Payload formatted", "ok", "The JSON payload is valid.");
  }

  function loadExample(example) {
    setKind(example.kind);
    setPayloadText(JSON.stringify(example.payload, null, 2));
    setCreateOutput(example.description);
    setActiveTab("create");
    addToast("Example loaded", "ok", example.title);
  }

  function previewExample(example) {
    setExampleOutput(
      pretty({
        title: example.title,
        kind: example.kind,
        description: example.description,
        payload: example.payload,
      }),
    );
    addToast("Example previewed", "ok", example.title);
  }

  function bundlePayload() {
    const payload = {
      style_dir: bundleState.style_dir,
      output: bundleState.output,
      name: bundleState.name,
      version: bundleState.version,
      source_mode: bundleState.source_mode,
      fetch_external_data: bundleState.fetch_external_data,
    };
    if (bundleState.pbf_uri) payload.pbf_uri = bundleState.pbf_uri;
    if (bundleState.poly_uri) payload.poly_uri = bundleState.poly_uri;
    if (bundleState.external_data_uri) {
      payload.external_data_uri = bundleState.external_data_uri;
      payload.external_data_source_mode = bundleState.source_mode;
    }
    return payload;
  }

  const bundleGate = gateForPayload("map-bundle/generate", bundlePayload(), capabilities);
  const diagnosticsCapabilities = diagnostics?.capabilities || capabilities;

  return (
    <div className="admin-shell">
      <aside className="sidebar">
        <div className="brand">
          <img src="/static/images/OpenTilesX.svg" alt="" />
          <div>
            <h1>OpenTilesX</h1>
            <p>Admin control plane</p>
          </div>
        </div>

        <form
          className="token-form"
          onSubmit={(event) => {
            event.preventDefault();
            saveToken();
          }}
        >
          <label htmlFor="token">Admin token</label>
          <input
            id="token"
            type="password"
            value={draftToken}
            onChange={(event) => setDraftToken(event.target.value)}
            placeholder="ADMIN_TOKEN"
          />
          <button className="full" type="submit">
            Save token
          </button>
        </form>

        <nav className="tabs" aria-label="Admin sections">
          {tabs.map(([value, label]) => (
            <button
              key={value}
              className={`tab ${activeTab === value ? "active" : ""}`}
              onClick={() => setActiveTab(value)}
            >
              {label}
            </button>
          ))}
        </nav>

        <a className="map-link" href="/map">
          Open map preview
        </a>
      </aside>

      <main className="workspace">
        <div className="workspace-shell">
          <header className="topbar">
            <div>
              <p className="eyebrow">Offline-ready operations</p>
              <h2>{tabs.find(([value]) => value === activeTab)?.[1]}</h2>
            </div>
          </header>

          <PolicyBanner capabilities={capabilities} />

          {activeTab === "dashboard" && (
            <section className="view-stack">
              <div className="cards">
                <article className="card">
                  <h3>Health</h3>
                  <pre>{health}</pre>
                </article>
                <article className="card">
                  <h3>Runtime</h3>
                  <dl className="facts">
                    <div>
                      <dt>Token</dt>
                      <dd>{token ? "saved" : "missing"}</dd>
                    </div>
                    <div>
                      <dt>External data mode</dt>
                      <dd>{capabilities?.external_data?.default_mode || "unknown"}</dd>
                    </div>
                    <div>
                      <dt>Fetch policy</dt>
                      <dd>
                        {capabilities
                          ? `${capabilities.external_data.allow_internal_downloads ? "internal on" : "internal off"} / ${
                              capabilities.external_data.allow_public_downloads ? "public on" : "public off"
                            }`
                          : "unknown"}
                      </dd>
                    </div>
                  </dl>
                </article>
                <article className="card">
                  <h3>Recent Status</h3>
                  <div className="status-counts">
                    {Object.entries(
                      jobs.reduce((memo, job) => {
                        memo[job.status || "unknown"] = (memo[job.status || "unknown"] || 0) + 1;
                        return memo;
                      }, {}),
                    ).map(([status, count]) => (
                      <span key={status} className={`count-pill ${status}`}>
                        {status}: {count}
                      </span>
                    ))}
                  </div>
                </article>
              </div>

              <section className="panel">
                <h3>Quick Actions</h3>
                <p>
                  Tiles appear after data is imported, the render worker consumes dirty tile records, and the API finds cached
                  PNGs in the shared filesystem or S3 tile store.
                </p>
                <div className="button-row">
                  <button onClick={() => setActiveTab("create")}>Create import job</button>
                  <button className="secondary" onClick={() => setActiveTab("jobs")}>
                    View jobs
                  </button>
                  <button className="quiet" onClick={() => setActiveTab("bundles")}>
                    Validate bundle
                  </button>
                  <button className="quiet with-icon" onClick={loadHealth}>
                    <RefreshIcon />
                    <span>Refresh dashboard</span>
                  </button>
                </div>
              </section>
            </section>
          )}

          {activeTab === "jobs" && (
            <section className="panel wide">
              <div className="panel-title-row">
                <div>
                  <h3>Jobs</h3>
                  <p>Search recent admin work without letting long ids or errors stretch the page sideways.</p>
                </div>
                <div className="toolbar">
                  <input value={jobLookup} onChange={(event) => setJobLookup(event.target.value)} placeholder="Paste a job id" />
                  <button className="secondary" onClick={() => openJob(jobLookup)}>
                    Open job
                  </button>
                  <select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}>
                    <option value="">All</option>
                    <option value="pending">Pending</option>
                    <option value="running">Running</option>
                    <option value="succeeded">Succeeded</option>
                    <option value="failed">Failed</option>
                  </select>
                  <button className="secondary with-icon" onClick={loadJobs}>
                    <RefreshIcon />
                    <span>Refresh</span>
                  </button>
                  <button
                    className="warn with-icon"
                    onClick={async () => {
                      if (!window.confirm("Clear succeeded and failed job history? Pending and running jobs will be kept.")) return;
                      try {
                        const data = await api("/admin/jobs/clear", token, {
                          method: "POST",
                          body: JSON.stringify({}),
                        });
                        setJobDetails(pretty(data));
                        addToast("Job history cleared", "ok", `${data.cleared?.deleted || 0} terminal jobs removed.`);
                        await loadJobs();
                      } catch (error) {
                        setJobDetails(pretty(error));
                        addToast("Job clear failed", "error", "Review the selected job panel.");
                      }
                    }}
                  >
                    <TrashIcon />
                    <span>Clear history</span>
                  </button>
                </div>
              </div>
              <div className="table-scroll">
                <table className="jobs-table">
                  <thead>
                    <tr>
                      <th>id</th>
                      <th>kind</th>
                      <th>status</th>
                      <th>created</th>
                      <th>error</th>
                    </tr>
                  </thead>
                  <tbody>
                    {jobs.length === 0 && (
                      <tr>
                        <td colSpan="5">No jobs found.</td>
                      </tr>
                    )}
                    {jobs.map((job) => (
                      <tr key={job.id}>
                        <td>
                          <div className="job-id-cell">
                            <button className="job-open" onClick={() => openJob(job.id)}>
                              {job.id}
                            </button>
                            <button
                              className="quiet copy-button icon-button"
                              type="button"
                              aria-label={`Copy job id ${job.id}`}
                              title="Copy job id"
                              onClick={() => {
                                copyText(job.id);
                                addToast("Copied", "ok", job.id);
                              }}
                            >
                              <CopyIcon />
                            </button>
                          </div>
                        </td>
                        <td>{job.kind}</td>
                        <td>
                          <span className={`status ${healthClass(job.status)}`}>{job.status}</span>
                        </td>
                        <td>{job.created_at}</td>
                        <td>{job.error}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <aside className="detail-drawer">
                <h4>Selected Job</h4>
                <pre>{jobDetails}</pre>
              </aside>
            </section>
          )}

          {activeTab === "create" && (
            <section className="page-grid">
              <article className="panel">
                <h3>Create Job</h3>
                <label htmlFor="kind">Job kind</label>
                <select
                  id="kind"
                  value={kind}
                  onChange={(event) => selectTemplate(event.target.value)}
                >
                  {Object.keys(endpoints).map((entry) => (
                    <option key={entry} value={entry}>
                      {entry}
                    </option>
                  ))}
                </select>
                <p className="hint">{descriptions[kind]}</p>
                {capabilities?.external_data?.default_mode === "placeholder" && (
                  <p className="gate-message">
                    {capabilities.external_data.default_mode_message}
                  </p>
                )}
                {createGate.blocked && <p className="gate-message blocked">{createGate.reason}</p>}
                <label htmlFor="payload">Payload JSON</label>
                <textarea
                  id="payload"
                  spellCheck="false"
                  value={payloadText}
                  onChange={(event) => setPayloadText(event.target.value)}
                />
                {parsedPayload.error && <p className="gate-message blocked">Invalid JSON: {parsedPayload.error}</p>}
                <div className="button-row">
                  <button
                    disabled={!token || !!parsedPayload.error || createGate.blocked}
                    onClick={() => createJob(endpoints[kind], parsedPayload.value || {}, setCreateOutput, kind)}
                  >
                    Create job
                  </button>
                  <button className="quiet" onClick={formatPayload}>
                    Format JSON
                  </button>
                </div>
              </article>

              <article className="panel response-panel">
                <h4>Create Job Response</h4>
                <pre>{createOutput}</pre>
              </article>
            </section>
          )}

          {activeTab === "bundles" && (
            <section className="page-grid">
              <div className="panel-stack">
                <article className="panel">
                  <h3>Bundle Generator</h3>
                  <p>Build a portable archive for airgap activation from a prepared style directory and optional import files.</p>
                  {bundleGate.blocked && <p className="gate-message blocked">{bundleGate.reason}</p>}
                  <div className="split">
                    <div>
                      <label>Name</label>
                      <input value={bundleState.name} onChange={(event) => setBundleState((state) => ({ ...state, name: event.target.value }))} />
                    </div>
                    <div>
                      <label>Version</label>
                      <input value={bundleState.version} onChange={(event) => setBundleState((state) => ({ ...state, version: event.target.value }))} />
                    </div>
                  </div>
                  <label>Style directory</label>
                  <input value={bundleState.style_dir} onChange={(event) => setBundleState((state) => ({ ...state, style_dir: event.target.value }))} />
                  <label>Output archive</label>
                  <input value={bundleState.output} onChange={(event) => setBundleState((state) => ({ ...state, output: event.target.value }))} />
                  <label>Optional PBF path</label>
                  <input value={bundleState.pbf_uri} onChange={(event) => setBundleState((state) => ({ ...state, pbf_uri: event.target.value }))} />
                  <label>Optional poly path</label>
                  <input value={bundleState.poly_uri} onChange={(event) => setBundleState((state) => ({ ...state, poly_uri: event.target.value }))} />
                  <label>Optional external-data preload path or archive</label>
                  <input
                    value={bundleState.external_data_uri}
                    onChange={(event) => setBundleState((state) => ({ ...state, external_data_uri: event.target.value }))}
                  />
                  <label>Bundle source mode</label>
                  <select
                    value={bundleState.source_mode}
                    onChange={(event) => setBundleState((state) => ({ ...state, source_mode: event.target.value }))}
                  >
                    <option value="local">local</option>
                    <option value="internal">internal</option>
                    <option value="public">public</option>
                  </select>
                  <label className="check">
                    <input
                      type="checkbox"
                      checked={bundleState.fetch_external_data}
                      onChange={(event) =>
                        setBundleState((state) => ({ ...state, fetch_external_data: event.target.checked }))
                      }
                    />
                    Fetch external data into the bundle
                  </label>
                  <p className="hint">
                    When checked, OpenTilesX fetches the style's external-data sources using the selected source mode and current policy.
                  </p>
                  <div className="button-row">
                    <button className="secondary" onClick={() => setBundleGenerateOutput(pretty(bundlePayload()))}>
                      Preview payload
                    </button>
                    <button
                      disabled={!token || bundleGate.blocked}
                      onClick={() => createJob(endpoints["map-bundle/generate"], bundlePayload(), setBundleGenerateOutput, "map-bundle/generate")}
                    >
                      Generate bundle
                    </button>
                  </div>
                </article>
                <article className="panel response-panel">
                  <h4>Bundle Generator Response</h4>
                  <pre>{bundleGenerateOutput}</pre>
                </article>
              </div>

              <div className="panel-stack">
                <article className="panel">
                  <h3>Validate Bundles</h3>
                  <label>Mounted or reachable bundle URI</label>
                  <input value={bundleValidateUri} onChange={(event) => setBundleValidateUri(event.target.value)} />
                  <div className="button-row">
                    <button
                      disabled={!token}
                      onClick={() => createJob(endpoints["map-bundle/validate"], { map_bundle_uri: bundleValidateUri }, setBundleValidateOutput, "map-bundle/validate")}
                    >
                      Validate mounted bundle
                    </button>
                  </div>
                  <div className="divider" />
                  <p>Upload a local archive to validate it immediately. The upload is temporary and is not activated.</p>
                  <label>Bundle archive</label>
                  <input type="file" onChange={(event) => setBundleUpload(event.target.files?.[0] || null)} />
                  <div className="button-row">
                    <button
                      className="secondary"
                      disabled={!token || !bundleUpload}
                      onClick={async () => {
                        const form = new FormData();
                        form.append("bundle", bundleUpload);
                        try {
                          const response = await fetch("/admin/map-bundle/validate-upload", {
                            method: "POST",
                            headers: { Authorization: `Bearer ${token}` },
                            body: form,
                          });
                          const data = await response.json();
                          setBundleValidateOutput(pretty(data));
                          addToast(data.valid ? "Bundle is valid" : "Bundle validation failed", data.valid ? "ok" : "error", bundleUpload.name);
                        } catch (error) {
                          setBundleValidateOutput(pretty(error));
                          addToast("Bundle upload failed", "error", "Review the validation response panel.");
                        }
                      }}
                    >
                      Upload and validate
                    </button>
                  </div>
                </article>
                <article className="panel response-panel">
                  <h4>Bundle Validation Response</h4>
                  <pre>{bundleValidateOutput}</pre>
                </article>
              </div>
            </section>
          )}

          {activeTab === "diagnostics" && (
            <section className="view-stack">
              <div className="panel-title-row">
                <div>
                  <h3>Render Diagnostics</h3>
                  <p>Check whether workers are alive, tiles are queued, failures are recent, and storage settings line up.</p>
                </div>
                <div className="toolbar">
                  <button className="secondary with-icon" onClick={loadDiagnostics}>
                    <RefreshIcon />
                    <span>Refresh diagnostics</span>
                  </button>
                  <button
                    className="warn with-icon"
                    onClick={async () => {
                      if (!window.confirm("Clear render-worker heartbeats and done/failed dirty-tile diagnostics? Pending and leased queue items will be kept.")) return;
                      try {
                        const data = await api("/admin/diagnostics/clear", token, {
                          method: "POST",
                          body: JSON.stringify({}),
                        });
                        setDiagnosticsOutput(pretty(data));
                        addToast(
                          "Diagnostics cleared",
                          "ok",
                          `${data.cleared?.heartbeats_deleted || 0} heartbeats and ${data.cleared?.dirty_tiles_deleted || 0} dirty-tile records removed.`,
                        );
                        await loadDiagnostics();
                      } catch (error) {
                        setDiagnosticsOutput(pretty(error));
                        addToast("Diagnostics clear failed", "error", "Review the diagnostics response panel.");
                      }
                    }}
                  >
                    <TrashIcon />
                    <span>Clear diagnostics</span>
                  </button>
                </div>
              </div>

              <div className="cards">
                <article className="card">
                  <h3>Render Workers</h3>
                  <dl className="facts">
                    <div>
                      <dt>Health</dt>
                      <dd>{diagnostics?.render_worker?.health || "unknown"}</dd>
                    </div>
                    <div>
                      <dt>State</dt>
                      <dd>{diagnostics ? `${diagnostics.render_worker?.healthy || 0}/${diagnostics.render_worker?.total || 0} healthy` : "unknown"}</dd>
                    </div>
                    <div>
                      <dt>Last seen</dt>
                      <dd>{diagnostics?.render_worker?.last_seen || "never"}</dd>
                    </div>
                    <div>
                      <dt>Processed</dt>
                      <dd>{diagnostics?.render_worker?.processed_count || 0}</dd>
                    </div>
                  </dl>
                </article>
                <article className="card">
                  <h3>Dirty Queue</h3>
                  <div className="status-counts">
                    {["pending", "leased", "done", "failed"].map((status) => (
                      <span key={status} className={`count-pill ${status}`}>
                        {status}: {diagnostics?.dirty_tiles?.counts?.[status] || 0}
                      </span>
                    ))}
                  </div>
                  <p className="hint">
                    {[diagnostics?.dirty_tiles?.oldest_pending_at && `Oldest pending: ${diagnostics.dirty_tiles.oldest_pending_at}`,
                      diagnostics?.dirty_tiles?.oldest_leased_at && `Oldest leased: ${diagnostics.dirty_tiles.oldest_leased_at}`]
                      .filter(Boolean)
                      .join(" | ")}
                  </p>
                </article>
                <article className="card">
                  <h3>External Data Policy</h3>
                  <pre>{pretty(diagnosticsCapabilities?.external_data || {})}</pre>
                </article>
              </div>

              <div className="page-grid">
                <article className="panel fixed-panel">
                  <h3>Worker Pool</h3>
                  <div className="table-scroll tall-scroll">
                    <table className="workers-table">
                      <thead>
                        <tr>
                          <th>worker</th>
                          <th>health</th>
                          <th>state</th>
                          <th>processed</th>
                          <th>timings</th>
                          <th>last seen</th>
                          <th>error</th>
                        </tr>
                      </thead>
                      <tbody>
                        {(diagnostics?.render_workers || []).map((worker) => (
                          <tr key={worker.worker_id || worker.hostname}>
                            <td>{worker.worker_id || worker.hostname}</td>
                            <td>{worker.health}</td>
                            <td>{worker.state}</td>
                            <td>{worker.processed_count}</td>
                            <td>render {worker.last_render_seconds || 0}s | store {worker.last_store_seconds || 0}s</td>
                            <td>{worker.last_seen}</td>
                            <td>{worker.last_error}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </article>

                <article className="panel fixed-panel">
                  <div className="panel-title-row">
                    <h3>Recent Failed Tiles</h3>
                    <div className="toolbar compact">
                      <label className="inline-label">Show</label>
                      <select value={failuresLimit} onChange={(event) => setFailuresLimit(Number(event.target.value))}>
                        <option value="10">10</option>
                        <option value="20">20</option>
                        <option value="50">50</option>
                        <option value="100">100</option>
                      </select>
                    </div>
                  </div>
                  <div className="table-scroll tall-scroll">
                    <table className="failures-table">
                      <thead>
                        <tr>
                          <th>tile</th>
                          <th>attempts</th>
                          <th>updated</th>
                          <th>error</th>
                        </tr>
                      </thead>
                      <tbody>
                        {(diagnostics?.recent_failures || []).map((failure) => (
                          <tr key={`${failure.layer}-${failure.z}-${failure.x}-${failure.y}`}>
                            <td>{failure.layer || "default"}/{failure.z}/{failure.x}/{failure.y}</td>
                            <td>{failure.attempts}</td>
                            <td>{failure.updated_at}</td>
                            <td>{failure.error}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </article>
              </div>

              <div className="page-grid">
                <article className="panel">
                  <h3>Runtime</h3>
                  <pre>{pretty({
                    rendering: diagnostics?.rendering || {},
                    storage: diagnostics?.storage || {},
                    external_data: diagnostics?.external_data || {},
                  })}</pre>
                </article>
                <article className="panel">
                  <h3>Test Render One Tile</h3>
                  <div className="split">
                    <div>
                      <label>Layer</label>
                      <input value={diagnosticTile.layer} onChange={(event) => setDiagnosticTile((state) => ({ ...state, layer: event.target.value }))} />
                    </div>
                    <div>
                      <label>Zoom</label>
                      <input type="number" value={diagnosticTile.z} onChange={(event) => setDiagnosticTile((state) => ({ ...state, z: Number(event.target.value) }))} />
                    </div>
                    <div>
                      <label>X</label>
                      <input type="number" value={diagnosticTile.x} onChange={(event) => setDiagnosticTile((state) => ({ ...state, x: Number(event.target.value) }))} />
                    </div>
                    <div>
                      <label>Y</label>
                      <input type="number" value={diagnosticTile.y} onChange={(event) => setDiagnosticTile((state) => ({ ...state, y: Number(event.target.value) }))} />
                    </div>
                  </div>
                  <div className="button-row">
                    <button
                      onClick={async () => {
                        try {
                          const data = await api("/admin/diagnostics/test-render", token, {
                            method: "POST",
                            body: JSON.stringify(diagnosticTile),
                          });
                          setDiagnosticsOutput(pretty(data));
                          addToast("Test tile queued", "ok", data.tile_url || `${diagnosticTile.z}/${diagnosticTile.x}/${diagnosticTile.y}`);
                          await loadDiagnostics();
                        } catch (error) {
                          setDiagnosticsOutput(pretty(error));
                          addToast("Test render failed", "error", "Review the diagnostics response panel.");
                        }
                      }}
                    >
                      Queue test tile
                    </button>
                  </div>
                  <p className="hint">This follows the normal render path. It queues the tile if missing; it does not render synchronously in the API.</p>
                  <pre>{diagnosticsOutput}</pre>
                </article>
              </div>
            </section>
          )}

          {activeTab === "examples" && (
            <section className="page-grid">
              <div className="panel">
                <h3>Examples</h3>
                <p>Each example explains the job and can be previewed before loading it into the Create Job form.</p>
                <div className="examples-grid">
                  {Object.entries(examples).map(([key, example]) => {
                    const gate = exampleGate(example, capabilities);
                    return (
                      <article key={key} className="example-card">
                        <h4>{example.title}</h4>
                        <p>{example.description}</p>
                        {gate.blocked && <p className="example-badge">{gate.reason}</p>}
                        <div className="button-row">
                          <button className="secondary" onClick={() => previewExample(example)}>
                            Preview
                          </button>
                          <button disabled={gate.blocked} onClick={() => loadExample(example)}>
                            Load into form
                          </button>
                        </div>
                      </article>
                    );
                  })}
                </div>
              </div>
              <article className="panel response-panel">
                <h4>Example Preview</h4>
                <pre>{exampleOutput}</pre>
              </article>
            </section>
          )}
        </div>
      </main>

      <div className="toast-stack" aria-live="polite">
        {toasts.map((toast) => (
          <div key={toast.id} className={`toast ${toast.type}`}>
            <div>
              <strong>{toast.message}</strong>
              {toast.detail && <span>{toast.detail}</span>}
            </div>
            <button className="toast-close" onClick={() => dismissToast(toast.id)} aria-label="Dismiss notification">
              x
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}

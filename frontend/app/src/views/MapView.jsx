import React, { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

const TILE_SIZE = 256;
const MAX_ZOOM = 22;
const MAX_TILE_ATTEMPTS = 8;
const DEFAULT_RETRY_SECONDS = 3;

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function worldSize(z) {
  return TILE_SIZE * Math.pow(2, z);
}

function tileKey(layer, z, x, y) {
  return `${layer}/${z}/${x}/${y}`;
}

function tileUrl(layer, z, x, y) {
  return layer !== "default"
    ? `/tile/${encodeURIComponent(layer)}/${z}/${x}/${y}.png`
    : `/tile/${z}/${x}/${y}.png`;
}

function parseRetryAfter(value) {
  if (!value) return DEFAULT_RETRY_SECONDS;
  const seconds = Number(value);
  if (Number.isFinite(seconds)) return clamp(seconds, 1, 30);
  const date = Date.parse(value);
  if (Number.isNaN(date)) return DEFAULT_RETRY_SECONDS;
  return clamp(Math.ceil((date - Date.now()) / 1000), 1, 30);
}

function centerTile(z, offsetX, offsetY, viewportSize) {
  const max = Math.pow(2, z) - 1;
  return {
    x: clamp(Math.floor((offsetX + viewportSize.width / 2) / TILE_SIZE), 0, max),
    y: clamp(Math.floor((offsetY + viewportSize.height / 2) / TILE_SIZE), 0, max),
  };
}

function initialTileState(reloadToken) {
  return {
    status: "loading",
    title: "Loading tile",
    detail: "Requesting PNG or render queue status.",
    attempts: 0,
    objectUrl: "",
    reloadToken,
  };
}

export default function MapView() {
  const viewportRef = useRef(null);
  const timersRef = useRef(new Map());
  const tileStatesRef = useRef({});
  const dragRef = useRef({
    dragging: false,
    pointerId: null,
    lastX: 0,
    lastY: 0,
    pinchDistance: null,
  });

  const [layer, setLayer] = useState("default");
  const [z, setZ] = useState(1);
  const [offsetX, setOffsetX] = useState(TILE_SIZE);
  const [offsetY, setOffsetY] = useState(TILE_SIZE);
  const [viewportSize, setViewportSize] = useState({ width: 0, height: 0 });
  const [tileStates, setTileStates] = useState({});
  const [dragging, setDragging] = useState(false);
  const [reloadToken, setReloadToken] = useState(0);

  useEffect(() => {
    tileStatesRef.current = tileStates;
  }, [tileStates]);

  const clampOffsets = useCallback(
    (nextX, nextY, nextZ = z) => {
      const maxOffset = worldSize(nextZ);
      return {
        x: clamp(nextX, 0, Math.max(0, maxOffset - viewportSize.width)),
        y: clamp(nextY, 0, Math.max(0, maxOffset - viewportSize.height)),
      };
    },
    [viewportSize.height, viewportSize.width, z],
  );

  useEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport) return undefined;
    const updateSize = () => {
      const rect = viewport.getBoundingClientRect();
      setViewportSize({ width: rect.width, height: rect.height });
    };
    updateSize();
    const observer = new ResizeObserver(updateSize);
    observer.observe(viewport);
    window.addEventListener("resize", updateSize);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", updateSize);
    };
  }, []);

  useLayoutEffect(() => {
    if (!viewportSize.width || !viewportSize.height) return;
    if (offsetX !== TILE_SIZE || offsetY !== TILE_SIZE) return;
    const centered = clampOffsets(
      Math.max(0, (worldSize(1) - viewportSize.width) / 2),
      Math.max(0, (worldSize(1) - viewportSize.height) / 2),
      1,
    );
    setOffsetX(centered.x);
    setOffsetY(centered.y);
  }, [clampOffsets, offsetX, offsetY, viewportSize.height, viewportSize.width]);

  useEffect(() => {
    if (!viewportSize.width || !viewportSize.height) return;
    const next = clampOffsets(offsetX, offsetY, z);
    if (next.x !== offsetX) setOffsetX(next.x);
    if (next.y !== offsetY) setOffsetY(next.y);
  }, [clampOffsets, offsetX, offsetY, viewportSize.height, viewportSize.width, z]);

  const visibleTiles = useMemo(() => {
    if (!viewportSize.width || !viewportSize.height) return [];
    const maxTile = Math.pow(2, z) - 1;
    const startX = clamp(Math.floor(offsetX / TILE_SIZE) - 1, 0, maxTile);
    const startY = clamp(Math.floor(offsetY / TILE_SIZE) - 1, 0, maxTile);
    const endX = clamp(Math.floor((offsetX + viewportSize.width) / TILE_SIZE) + 1, 0, maxTile);
    const endY = clamp(Math.floor((offsetY + viewportSize.height) / TILE_SIZE) + 1, 0, maxTile);
    const tiles = [];
    for (let y = startY; y <= endY; y += 1) {
      for (let x = startX; x <= endX; x += 1) {
        tiles.push({
          key: tileKey(layer, z, x, y),
          layer,
          z,
          x,
          y,
          left: x * TILE_SIZE - offsetX,
          top: y * TILE_SIZE - offsetY,
        });
      }
    }
    return tiles;
  }, [layer, offsetX, offsetY, viewportSize.height, viewportSize.width, z]);

  const updateTileState = useCallback((key, updater) => {
    setTileStates((current) => {
      const existing = current[key] || initialTileState(reloadToken);
      const next = typeof updater === "function" ? updater(existing) : updater;
      if (!next) return current;
      return {
        ...current,
        [key]: { ...existing, ...next },
      };
    });
  }, [reloadToken]);

  const clearRetry = useCallback((key) => {
    const timer = timersRef.current.get(key);
    if (timer) window.clearTimeout(timer);
    timersRef.current.delete(key);
  }, []);

  const revokeTileUrl = useCallback((key) => {
    const existing = tileStatesRef.current[key]?.objectUrl;
    if (existing) URL.revokeObjectURL(existing);
  }, []);

  const loadTile = useCallback(
    async (descriptor, attempts = 0) => {
      const key = descriptor.key;
      clearRetry(key);
      revokeTileUrl(key);
      updateTileState(key, {
        status: "loading",
        title: "Loading tile",
        detail: `Attempt ${attempts + 1} for ${descriptor.z}/${descriptor.x}/${descriptor.y}.`,
        attempts: attempts + 1,
        objectUrl: "",
        reloadToken,
      });

      try {
        const response = await fetch(
          `${tileUrl(descriptor.layer, descriptor.z, descriptor.x, descriptor.y)}?preview=${Date.now()}`,
          { headers: { Accept: "image/png, application/json" } },
        );
        const contentType = response.headers.get("Content-Type") || "";
        if (response.ok && contentType.includes("image/png")) {
          const blob = await response.blob();
          const objectUrl = URL.createObjectURL(blob);
          updateTileState(key, {
            status: "ready",
            title: "Ready",
            detail: `${descriptor.z}/${descriptor.x}/${descriptor.y}`,
            objectUrl,
            reloadToken,
          });
          return;
        }
        if (response.status === 202) {
          let title = "Queued for rendering";
          try {
            const payload = await response.json();
            if (payload.status) title = `${payload.status} for rendering`;
          } catch (_) {}
          const retrySeconds = parseRetryAfter(response.headers.get("Retry-After"));
          if (attempts + 1 >= MAX_TILE_ATTEMPTS) {
            updateTileState(key, {
              status: "queued",
              title,
              detail: "Retry limit reached. Use Retry queued after confirming render workers are healthy.",
              reloadToken,
            });
            return;
          }
          updateTileState(key, {
            status: "queued",
            title,
            detail: `Retrying in ${retrySeconds}s. Import, render workers, and shared tile storage must be healthy.`,
            reloadToken,
          });
          const timer = window.setTimeout(() => {
            timersRef.current.delete(key);
            if (tileStatesRef.current[key]) loadTile(descriptor, attempts + 1);
          }, retrySeconds * 1000);
          timersRef.current.set(key, timer);
          return;
        }
        updateTileState(key, {
          status: "error",
          title: `HTTP ${response.status}`,
          detail: "Tile request failed. Check API, storage, and renderer diagnostics.",
          reloadToken,
        });
      } catch (error) {
        updateTileState(key, {
          status: "error",
          title: "Request failed",
          detail: String(error),
          reloadToken,
        });
      }
    },
    [clearRetry, reloadToken, revokeTileUrl, updateTileState],
  );

  useEffect(() => {
    const wanted = new Set(visibleTiles.map((tile) => tile.key));
    Object.keys(tileStatesRef.current).forEach((key) => {
      if (!wanted.has(key)) {
        clearRetry(key);
        revokeTileUrl(key);
      }
    });

    setTileStates((current) => {
      const next = {};
      visibleTiles.forEach((tile) => {
        next[tile.key] =
          current[tile.key] && current[tile.key].reloadToken === reloadToken
            ? current[tile.key]
            : initialTileState(reloadToken);
      });
      return next;
    });

    visibleTiles.forEach((tile) => {
      const existing = tileStatesRef.current[tile.key];
      if (!existing || existing.reloadToken !== reloadToken) {
        loadTile(tile, 0);
      }
    });
  }, [clearRetry, loadTile, reloadToken, revokeTileUrl, visibleTiles]);

  useEffect(
    () => () => {
      Array.from(timersRef.current.keys()).forEach(clearRetry);
      Object.keys(tileStatesRef.current).forEach(revokeTileUrl);
    },
    [clearRetry, revokeTileUrl],
  );

  const counts = useMemo(() => {
    const summary = { ready: 0, queued: 0, error: 0, loading: 0 };
    visibleTiles.forEach((tile) => {
      const status = tileStates[tile.key]?.status || "loading";
      summary[status] = (summary[status] || 0) + 1;
    });
    return summary;
  }, [tileStates, visibleTiles]);

  const center = useMemo(
    () => centerTile(z, offsetX, offsetY, viewportSize),
    [offsetX, offsetY, viewportSize, z],
  );

  const statusText = useMemo(
    () =>
      `Layer ${layer} around ${z}/${center.x}/${center.y}. ${counts.ready} ready, ${counts.queued} queued, ${counts.error} error, ${counts.loading} loading.`,
    [center.x, center.y, counts.error, counts.loading, counts.queued, counts.ready, layer, z],
  );

  const zoomBy = useCallback(
    (delta, anchorX = null, anchorY = null) => {
      if (!viewportSize.width || !viewportSize.height) return;
      setZ((current) => {
        const next = clamp(current + delta, 0, MAX_ZOOM);
        if (next === current) return current;
        const localX = anchorX == null ? viewportSize.width / 2 : anchorX;
        const localY = anchorY == null ? viewportSize.height / 2 : anchorY;
        const worldX = offsetX + localX;
        const worldY = offsetY + localY;
        const scale = Math.pow(2, next - current);
        const nextOffsets = clampOffsets(worldX * scale - localX, worldY * scale - localY, next);
        setOffsetX(nextOffsets.x);
        setOffsetY(nextOffsets.y);
        setReloadToken((value) => value + 1);
        return next;
      });
    },
    [clampOffsets, offsetX, offsetY, viewportSize.height, viewportSize.width],
  );

  const resetWorld = useCallback(() => {
    if (!viewportSize.width || !viewportSize.height) return;
    const nextZ = 1;
    const centered = clampOffsets(
      Math.max(0, (worldSize(nextZ) - viewportSize.width) / 2),
      Math.max(0, (worldSize(nextZ) - viewportSize.height) / 2),
      nextZ,
    );
    setZ(nextZ);
    setOffsetX(centered.x);
    setOffsetY(centered.y);
    setReloadToken((value) => value + 1);
  }, [clampOffsets, viewportSize.height, viewportSize.width]);

  const reloadTiles = useCallback(() => {
    const clamped = clampOffsets(offsetX, offsetY, z);
    setOffsetX(clamped.x);
    setOffsetY(clamped.y);
    setReloadToken((value) => value + 1);
  }, [clampOffsets, offsetX, offsetY, z]);

  const retryQueuedTiles = useCallback(() => {
    visibleTiles.forEach((tile) => {
      const status = tileStatesRef.current[tile.key]?.status;
      if (status === "queued" || status === "error") {
        loadTile(tile, 0);
      }
    });
  }, [loadTile, visibleTiles]);

  const onPointerDown = useCallback((event) => {
    dragRef.current.dragging = true;
    dragRef.current.pointerId = event.pointerId;
    dragRef.current.lastX = event.clientX;
    dragRef.current.lastY = event.clientY;
    setDragging(true);
    event.currentTarget.setPointerCapture(event.pointerId);
  }, []);

  const onPointerMove = useCallback(
    (event) => {
      if (!dragRef.current.dragging || event.pointerId !== dragRef.current.pointerId) return;
      const deltaX = event.clientX - dragRef.current.lastX;
      const deltaY = event.clientY - dragRef.current.lastY;
      dragRef.current.lastX = event.clientX;
      dragRef.current.lastY = event.clientY;
      const next = clampOffsets(offsetX - deltaX, offsetY - deltaY, z);
      setOffsetX(next.x);
      setOffsetY(next.y);
    },
    [clampOffsets, offsetX, offsetY, z],
  );

  const stopDragging = useCallback((event) => {
    if (event.pointerId !== dragRef.current.pointerId) return;
    dragRef.current.dragging = false;
    dragRef.current.pointerId = null;
    setDragging(false);
  }, []);

  const onWheel = useCallback(
    (event) => {
      event.preventDefault();
      const rect = viewportRef.current?.getBoundingClientRect();
      if (!rect) return;
      zoomBy(event.deltaY < 0 ? 1 : -1, event.clientX - rect.left, event.clientY - rect.top);
    },
    [zoomBy],
  );

  const touchDistance = (touches) =>
    Math.hypot(touches[0].clientX - touches[1].clientX, touches[0].clientY - touches[1].clientY);

  const touchCenter = (touches) => {
    const rect = viewportRef.current?.getBoundingClientRect();
    return {
      x: (touches[0].clientX + touches[1].clientX) / 2 - (rect?.left || 0),
      y: (touches[0].clientY + touches[1].clientY) / 2 - (rect?.top || 0),
    };
  };

  const onTouchStart = useCallback((event) => {
    if (event.touches.length !== 2) return;
    dragRef.current.dragging = false;
    dragRef.current.pinchDistance = touchDistance(event.touches);
    setDragging(false);
  }, []);

  const onTouchMove = useCallback(
    (event) => {
      if (!dragRef.current.pinchDistance || event.touches.length !== 2) return;
      const distance = touchDistance(event.touches);
      const centerPoint = touchCenter(event.touches);
      if (distance > dragRef.current.pinchDistance * 1.22) {
        zoomBy(1, centerPoint.x, centerPoint.y);
        dragRef.current.pinchDistance = distance;
      } else if (distance < dragRef.current.pinchDistance * 0.82) {
        zoomBy(-1, centerPoint.x, centerPoint.y);
        dragRef.current.pinchDistance = distance;
      }
    },
    [zoomBy],
  );

  const onTouchEnd = useCallback((event) => {
    if (event.touches.length < 2) dragRef.current.pinchDistance = null;
  }, []);

  return (
    <div className="map-route">
      <header className="map-bar">
        <div>
          <p className="eyebrow">OpenTilesX</p>
          <h1>Map Preview</h1>
        </div>
          <div className="map-header-actions">
            <img className="map-header-logo" src="/static/images/OpenTilesX.svg" alt="" />
            <a href="/">Home</a>
            <a href="/admin">Admin</a>
          </div>
      </header>

      <main className="map-layout">
        <aside className="map-controls panel">
          <label htmlFor="map-layer">Layer</label>
          <input id="map-layer" value={layer} onChange={(event) => setLayer(event.target.value || "default")} />

          <div className="split">
            <div>
              <label htmlFor="map-zoom">Zoom</label>
              <input
                id="map-zoom"
                type="number"
                value={z}
                min="0"
                max={MAX_ZOOM}
                onChange={(event) => setZ(clamp(Number(event.target.value) || 0, 0, MAX_ZOOM))}
              />
            </div>
            <div>
              <label htmlFor="map-center">Center</label>
              <input id="map-center" readOnly value={`${z}/${center.x}/${center.y}`} />
            </div>
          </div>

          <div className="button-grid">
            <button onClick={() => zoomBy(-1)}>Zoom out</button>
            <button onClick={() => zoomBy(1)}>Zoom in</button>
            <button onClick={resetWorld}>World</button>
            <button onClick={reloadTiles}>Reload tiles</button>
            <button className="secondary" onClick={retryQueuedTiles}>
              Retry queued
            </button>
          </div>

          <p>Drag to pan. Use the mouse wheel or touch gestures to zoom. Only this server&apos;s tile endpoints are loaded.</p>
          <div className="status-counters" aria-label="Visible tile status">
            <span className="count-pill ready">
              ready: <strong>{counts.ready}</strong>
            </span>
            <span className="count-pill queued">
              queued: <strong>{counts.queued}</strong>
            </span>
            <span className="count-pill error">
              error: <strong>{counts.error}</strong>
            </span>
          </div>
          <p className="hint">
            Queued tiles need a successful import, healthy render workers, and matching shared filesystem or S3 storage
            between the API and renderer.
          </p>
          <pre>{statusText}</pre>
        </aside>

        <section
          ref={viewportRef}
          className={`map-shell ${dragging ? "dragging" : ""}`}
          aria-label="Interactive map preview"
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={stopDragging}
          onPointerCancel={stopDragging}
          onWheel={onWheel}
          onTouchStart={onTouchStart}
          onTouchMove={onTouchMove}
          onTouchEnd={onTouchEnd}
          onTouchCancel={onTouchEnd}
        >
          <div className="tile-layer">
            {visibleTiles.map((tile) => {
              const state = tileStates[tile.key] || initialTileState(reloadToken);
              return (
                <div
                  key={tile.key}
                  className={`tile ${state.status}`}
                  style={{ left: `${tile.left}px`, top: `${tile.top}px` }}
                >
                  {state.status === "ready" && state.objectUrl ? (
                    <img alt={`${tile.z}/${tile.x}/${tile.y}`} draggable="false" src={state.objectUrl} />
                  ) : (
                    <div className="placeholder">
                      <div>
                        <strong>{state.title}</strong>
                        <small>{state.detail}</small>
                      </div>
                    </div>
                  )}
                  <span className="coord">
                    {tile.z}/{tile.x}/{tile.y}
                  </span>
                </div>
              );
            })}
          </div>
          <div className="overlay">
            z{z} x{center.x} y{center.y}
          </div>
        </section>
      </main>
    </div>
  );
}

from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from .config import AppConfig
from .jobs import JobStore
from .storage import TileStorage
from .tiles import TileError, TileRef


JOB_ENDPOINTS = {
    "/admin/jobs/import": "import",
    "/admin/jobs/update": "update",
    "/admin/jobs/reimport": "reimport",
    "/admin/jobs/expire": "expire",
    "/admin/jobs/map-bundle/validate": "bundle_validate",
    "/admin/jobs/map-bundle/activate": "bundle_activate",
}


class TileApiHandler(BaseHTTPRequestHandler):
    config: AppConfig
    storage: TileStorage
    store: JobStore

    server_version = "TileServer/0.1"

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/healthz":
            self._json({"status": "ok", "role": self.config.role, "tile_store": self.config.tile_store})
            return
        if path == "/metrics":
            self._text("tile_server_up 1\n", content_type="text/plain; version=0.0.4")
            return
        if path == "/admin":
            self._admin_panel()
            return
        if path.startswith("/tile/"):
            self._serve_tile(path)
            return
        if path.startswith("/admin/jobs/"):
            self._admin_get_job(path)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path in JOB_ENDPOINTS:
            self._admin_create_job(path)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}")

    def _serve_tile(self, path: str) -> None:
        try:
            segments = [part for part in path.removeprefix("/tile/").split("/") if part]
            tile = TileRef.from_segments(self.config.default_layer, segments)
        except TileError as exc:
            self.send_error(HTTPStatus.BAD_REQUEST, str(exc))
            return

        obj = self.storage.get(tile)
        if obj:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", obj.content_type)
            self.send_header("Cache-Control", "public, max-age=3600")
            self.send_header("Content-Length", str(len(obj.data)))
            self.end_headers()
            self.wfile.write(obj.data)
            return

        try:
            self.store.enqueue_dirty_tile(tile)
        except Exception as exc:
            self._json({"error": f"Tile missing and render enqueue failed: {exc}"}, status=HTTPStatus.SERVICE_UNAVAILABLE)
            return

        self._json({"status": "queued", "tile": tile.__dict__}, status=HTTPStatus.ACCEPTED, headers={"Retry-After": "3"})

    def _admin_create_job(self, path: str) -> None:
        if not self._authorized():
            return
        payload = self._read_json()
        kind = JOB_ENDPOINTS[path]
        try:
            job_id = self.store.create_job(kind, payload)
        except Exception as exc:
            self._json({"error": str(exc)}, status=HTTPStatus.SERVICE_UNAVAILABLE)
            return
        self._json({"id": job_id, "kind": kind, "status": "pending"}, status=HTTPStatus.ACCEPTED)

    def _admin_get_job(self, path: str) -> None:
        if not self._authorized():
            return
        job_id = path.rsplit("/", 1)[-1]
        try:
            job = self.store.get_job(job_id)
        except Exception as exc:
            self._json({"error": str(exc)}, status=HTTPStatus.SERVICE_UNAVAILABLE)
            return
        if not job:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self._json(
            {
                "id": job.id,
                "kind": job.kind,
                "status": job.status,
                "payload": job.payload,
                "result": job.result,
                "error": job.error,
            }
        )

    def _admin_panel(self) -> None:
        if not self.config.admin_enabled:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self._text(ADMIN_HTML, content_type="text/html; charset=utf-8")

    def _authorized(self) -> bool:
        if not self.config.admin_enabled:
            self.send_error(HTTPStatus.NOT_FOUND)
            return False
        expected = f"Bearer {self.config.admin_token}"
        if self.headers.get("Authorization") != expected:
            self._json({"error": "Unauthorized"}, status=HTTPStatus.UNAUTHORIZED)
            return False
        return True

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length == 0:
            return {}
        if length > 2 * 1024 * 1024:
            raise ValueError("Request body is too large")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _json(
        self,
        payload: dict[str, Any],
        status: HTTPStatus = HTTPStatus.OK,
        headers: dict[str, str] | None = None,
    ) -> None:
        data = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def _text(self, payload: str, content_type: str = "text/plain") -> None:
        data = payload.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


ADMIN_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Tile Admin</title>
  <style>
    body { margin: 0; font-family: system-ui, sans-serif; color: #161616; background: #f7f7f4; }
    main { max-width: 960px; margin: 0 auto; padding: 28px; }
    h1 { font-size: 28px; margin: 0 0 16px; }
    h2 { font-size: 18px; margin: 24px 0 10px; }
    label { display: block; font-weight: 700; margin: 12px 0 6px; }
    input, textarea, select, button { font: inherit; border-radius: 6px; }
    input, textarea, select { box-sizing: border-box; width: 100%; border: 1px solid #999; padding: 10px; background: #fff; }
    textarea { min-height: 140px; }
    button { border: 0; padding: 10px 14px; background: #126149; color: #fff; cursor: pointer; }
    button.secondary { background: #454b4a; }
    .row { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
    .panel { border: 1px solid #d0d0c8; border-radius: 8px; padding: 16px; background: #fff; margin-top: 16px; }
    pre { white-space: pre-wrap; overflow-wrap: anywhere; background: #101614; color: #e7f4ee; padding: 14px; border-radius: 8px; }
    @media (max-width: 720px) { .row { grid-template-columns: 1fr; } main { padding: 18px; } }
  </style>
</head>
<body>
<main>
  <h1>Tile Admin</h1>
  <div class="panel">
    <label for="token">Admin token</label>
    <input id="token" type="password" autocomplete="current-password">
  </div>
  <div class="row">
    <section class="panel">
      <h2>Create job</h2>
      <label for="kind">Job kind</label>
      <select id="kind">
        <option value="import">import</option>
        <option value="update">update</option>
        <option value="reimport">reimport</option>
        <option value="expire">expire</option>
        <option value="map-bundle/validate">map-bundle/validate</option>
        <option value="map-bundle/activate">map-bundle/activate</option>
      </select>
      <label for="payload">Payload JSON</label>
      <textarea id="payload">{}</textarea>
      <button onclick="createJob()">Create job</button>
    </section>
    <section class="panel">
      <h2>Job status</h2>
      <label for="jobid">Job id</label>
      <input id="jobid">
      <button class="secondary" onclick="getJob()">Get status</button>
    </section>
  </div>
  <pre id="output">Ready.</pre>
</main>
<script>
async function request(path, options) {
  const token = document.getElementById('token').value;
  const response = await fetch(path, {
    ...options,
    headers: {
      'Authorization': 'Bearer ' + token,
      'Content-Type': 'application/json',
      ...(options && options.headers ? options.headers : {})
    }
  });
  const text = await response.text();
  try { return JSON.stringify(JSON.parse(text), null, 2); }
  catch (_) { return text; }
}
async function createJob() {
  const kind = document.getElementById('kind').value;
  const payload = document.getElementById('payload').value || '{}';
  document.getElementById('output').textContent =
    await request('/admin/jobs/' + kind, { method: 'POST', body: payload });
}
async function getJob() {
  const id = document.getElementById('jobid').value.trim();
  document.getElementById('output').textContent =
    await request('/admin/jobs/' + encodeURIComponent(id), { method: 'GET' });
}
</script>
</body>
</html>
"""


def run_server(config: AppConfig, storage: TileStorage, store: JobStore) -> None:
    handler = type(
        "ConfiguredTileApiHandler",
        (TileApiHandler,),
        {"config": config, "storage": storage, "store": store},
    )
    server = ThreadingHTTPServer((config.listen_host, config.listen_port), handler)
    print(f"tile-api listening on {config.listen_host}:{config.listen_port}")
    server.serve_forever()

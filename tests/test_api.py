import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path

from tests.helpers import minimal_config
from tile_server.api import TileApiHandler
from tile_server.storage import TileObject


class _Store:
    def __init__(self):
        self.created = []
        self.enqueued = []
        self.cleared_jobs = 0
        self.cleared_diagnostics = 0
        self.last_failures_limit = None

    def create_job(self, kind, payload):
        self.created.append((kind, payload))
        return "job-1"

    def list_jobs(self, limit=50, status=None):
        return [{"id": "job-1", "kind": "import", "status": status or "pending", "payload": {}, "result": {}, "error": None}]

    def get_job(self, job_id):
        return None

    def get_setting(self, key):
        if key == "render_worker_heartbeat":
            return {
                "worker_id": "render-worker-1",
                "last_seen": "2099-01-01T00:00:00+00:00",
                "state": "idle",
                "processed_count": 7,
                "render_backend": "python-mapnik",
                "last_render_seconds": 0.25,
                "last_store_seconds": 0.05,
                "tile_store": "filesystem",
                "map_version": "default",
            }
        return None

    def render_worker_heartbeats(self):
        return [
            {
                "worker_id": "render-worker-1",
                "last_seen": "2099-01-01T00:00:00+00:00",
                "state": "idle",
                "processed_count": 7,
                "render_backend": "python-mapnik",
                "last_render_seconds": 0.25,
                "last_store_seconds": 0.05,
                "tile_store": "filesystem",
                "map_version": "default",
            },
            {
                "worker_id": "render-worker-2",
                "last_seen": "2099-01-01T00:00:00+00:00",
                "state": "processed",
                "processed_count": 11,
                "render_backend": "python-mapnik",
                "last_render_seconds": 0.13,
                "last_store_seconds": 0.03,
                "tile_store": "filesystem",
                "map_version": "default",
            },
        ]

    def dirty_tile_counts(self):
        return {"counts": {"pending": 2, "failed": 1}, "oldest_pending_at": "2026-01-01T00:00:00+00:00", "oldest_leased_at": None}

    def recent_failed_dirty_tiles(self, limit=20):
        self.last_failures_limit = limit
        return [{"layer": "default", "z": 1, "x": 0, "y": 0, "attempts": 1, "error": "mapnik failed", "updated_at": "2026-01-01T00:00:00+00:00"}]

    def enqueue_dirty_tile(self, tile, reason="missing"):
        self.enqueued.append((tile, reason))
        return True

    def clear_jobs(self):
        self.cleared_jobs += 1
        return {"deleted": 2, "by_status": {"succeeded": 1, "failed": 1}}

    def clear_diagnostics(self):
        self.cleared_diagnostics += 1
        return {"heartbeats_deleted": 2, "dirty_tiles_deleted": 3, "dirty_tiles_by_status": {"done": 2, "failed": 1}}


class _Storage:
    def __init__(self, exists=False):
        self._exists = exists

    def exists(self, tile):
        return self._exists

    def get(self, tile):
        if self._exists:
            return TileObject(b"png")
        return None


class _Handler(TileApiHandler):
    def __init__(self, config, store, path="/admin/jobs", token=None, payload=None, headers=None, body=b""):
        self.config = config
        self.store = store
        self.storage = _Storage()
        self.path = path
        self.payload = payload or {}
        self.headers = headers or {}
        if token:
            self.headers["Authorization"] = f"Bearer {token}"
        self.rfile = io.BytesIO(body)
        self.response = None
        self.error = None
        self.text = None
        self.bytes = None

    def _json(self, payload, status=200, headers=None):
        self.response = {"status": int(status), "payload": payload, "headers": headers or {}}

    def send_error(self, status, message=None):
        self.error = {"status": int(status), "message": message}

    def _read_json(self):
        return self.payload

    def _text(self, payload, content_type="text/plain"):
        self.text = {"payload": payload, "content_type": content_type}

    def _bytes(self, payload, content_type, status=200, headers=None):
        self.bytes = {
            "payload": payload,
            "text": payload.decode("utf-8"),
            "content_type": content_type,
            "status": int(status),
            "headers": headers or {},
        }


class AdminApiTests(unittest.TestCase):
    def test_root_panel_serves_static_html_even_when_admin_disabled(self):
        handler = _Handler(minimal_config(admin_enabled=False), _Store())
        handler.path = "/"
        handler._home_panel()
        self.assertIn('id="root"', handler.bytes["text"])
        self.assertIn("/static/web-ui/assets/", handler.bytes["text"])

    def test_admin_disabled_returns_404(self):
        handler = _Handler(minimal_config(admin_enabled=False), _Store())
        handler._admin_panel()
        self.assertEqual(handler.error["status"], 404)

    def test_admin_jobs_require_token(self):
        handler = _Handler(minimal_config(admin_enabled=True, admin_token="secret"), _Store())
        handler._admin_list_jobs()
        self.assertEqual(handler.response["status"], 401)

    def test_admin_can_list_and_create_jobs(self):
        store = _Store()
        config = minimal_config(admin_enabled=True, admin_token="secret")

        list_handler = _Handler(config, store, path="/admin/jobs?status=pending", token="secret")
        list_handler._admin_list_jobs()

        create_handler = _Handler(
            config,
            store,
            path="/admin/jobs/map-bundle/generate",
            token="secret",
            payload={"style_dir": "/style", "output": "/bundle.tar.gz"},
        )
        create_handler._admin_create_job("/admin/jobs/map-bundle/generate")

        self.assertEqual(list_handler.response["payload"]["jobs"][0]["id"], "job-1")
        self.assertEqual(create_handler.response["payload"]["kind"], "bundle_generate")
        self.assertEqual(store.created[0][0], "bundle_generate")

    def test_admin_capabilities_require_token(self):
        handler = _Handler(minimal_config(admin_enabled=True, admin_token="secret"), _Store())
        handler._admin_capabilities()
        self.assertEqual(handler.response["status"], 401)

    def test_admin_capabilities_return_external_data_policy(self):
        handler = _Handler(
            minimal_config(
                admin_enabled=True,
                admin_token="secret",
                external_data_mode="placeholder",
                allow_internal_external_data_downloads=False,
                allow_public_external_data_downloads=True,
            ),
            _Store(),
            token="secret",
        )
        handler._admin_capabilities()
        payload = handler.response["payload"]
        self.assertEqual(payload["external_data"]["default_mode"], "placeholder")
        self.assertFalse(payload["external_data"]["allow_internal_downloads"])
        self.assertTrue(payload["external_data"]["allow_public_downloads"])
        self.assertFalse(payload["actions"]["import"]["fetch_internal"]["allowed"])
        self.assertTrue(payload["actions"]["import"]["fetch_public"]["allowed"])

    def test_admin_create_job_rejects_blocked_external_data_fetch(self):
        store = _Store()
        handler = _Handler(
            minimal_config(
                admin_enabled=True,
                admin_token="secret",
                allow_internal_external_data_downloads=False,
                allow_public_external_data_downloads=False,
            ),
            store,
            token="secret",
            payload={
                "pbf_uri": "/data/import/region.osm.pbf",
                "external_data_mode": "fetch",
                "external_data_source_mode": "public",
            },
        )
        handler._admin_create_job("/admin/jobs/import")
        self.assertEqual(handler.response["status"], 400)
        self.assertEqual(store.created, [])

    def test_admin_create_job_rejects_blocked_external_data_fetch_from_default_mode(self):
        store = _Store()
        handler = _Handler(
            minimal_config(
                admin_enabled=True,
                admin_token="secret",
                external_data_mode="fetch",
                allow_public_external_data_downloads=False,
            ),
            store,
            token="secret",
            payload={
                "pbf_uri": "/data/import/region.osm.pbf",
                "external_data_source_mode": "public",
            },
        )
        handler._admin_create_job("/admin/jobs/import")
        self.assertEqual(handler.response["status"], 400)
        self.assertEqual(store.created, [])

    def test_admin_create_job_rejects_remote_external_data_preload_when_public_downloads_disabled(self):
        store = _Store()
        handler = _Handler(
            minimal_config(
                admin_enabled=True,
                admin_token="secret",
                allow_public_external_data_downloads=False,
            ),
            store,
            token="secret",
            payload={
                "pbf_uri": "/data/import/region.osm.pbf",
                "external_data_mode": "local",
                "external_data_uri": "https://mirror.example/offline-preload.tar.gz",
                "external_data_source_mode": "public",
            },
        )
        handler._admin_create_job("/admin/jobs/import")
        self.assertEqual(handler.response["status"], 400)
        self.assertEqual(store.created, [])

    def test_bundle_generate_rejects_remote_external_data_preload_with_local_source_mode(self):
        store = _Store()
        handler = _Handler(
            minimal_config(admin_enabled=True, admin_token="secret"),
            store,
            token="secret",
            payload={
                "style_dir": "/style",
                "output": "/bundle.tar.gz",
                "source_mode": "local",
                "external_data_uri": "https://mirror.example/offline-preload.tar.gz",
            },
        )
        handler._admin_create_job("/admin/jobs/map-bundle/generate")
        self.assertEqual(handler.response["status"], 400)
        self.assertEqual(store.created, [])

    def test_admin_create_job_allows_local_external_data_payload(self):
        store = _Store()
        handler = _Handler(
            minimal_config(admin_enabled=True, admin_token="secret"),
            store,
            token="secret",
            payload={
                "pbf_uri": "/data/import/region.osm.pbf",
                "external_data_mode": "local",
                "external_data_uri": "/data/external-data/osm-carto-cache",
                "external_data_source_mode": "local",
            },
        )
        handler._admin_create_job("/admin/jobs/import")
        self.assertEqual(handler.response["status"], 202)
        self.assertEqual(store.created[0][0], "import")

    def test_admin_diagnostics_requires_token(self):
        handler = _Handler(minimal_config(admin_enabled=True, admin_token="secret"), _Store())
        handler._admin_diagnostics()
        self.assertEqual(handler.response["status"], 401)

    def test_admin_diagnostics_returns_render_queue_and_storage_sections(self):
        store = _Store()
        handler = _Handler(minimal_config(admin_enabled=True, admin_token="secret"), store, path="/admin/diagnostics?failures_limit=50", token="secret")
        handler._admin_diagnostics()
        payload = handler.response["payload"]
        self.assertIn("render_worker", payload)
        self.assertEqual(payload["render_worker"]["health"], "healthy")
        self.assertEqual(payload["render_worker"]["total"], 2)
        self.assertEqual(payload["render_worker"]["processed_count"], 18)
        self.assertEqual(len(payload["render_workers"]), 2)
        self.assertIn("dirty_tiles", payload)
        self.assertEqual(payload["dirty_tiles"]["counts"]["pending"], 2)
        self.assertIn("recent_failures", payload)
        self.assertIn("rendering", payload)
        self.assertEqual(payload["rendering"]["backend"], "python-mapnik")
        self.assertIn("external_data", payload)
        self.assertIn("storage", payload)
        self.assertEqual(payload["storage"]["type"], "filesystem")
        self.assertEqual(payload["recent_failures_limit"], 50)
        self.assertEqual(store.last_failures_limit, 50)

    def test_admin_diagnostics_clamps_failures_limit(self):
        store = _Store()
        handler = _Handler(minimal_config(admin_enabled=True, admin_token="secret"), store, path="/admin/diagnostics?failures_limit=999", token="secret")
        handler._admin_diagnostics()
        self.assertEqual(handler.response["payload"]["recent_failures_limit"], 100)
        self.assertEqual(store.last_failures_limit, 100)

    def test_admin_test_render_validates_and_enqueues_tile(self):
        store = _Store()
        handler = _Handler(
            minimal_config(admin_enabled=True, admin_token="secret"),
            store,
            token="secret",
            payload={"layer": "default", "z": 0, "x": 0, "y": 0},
        )
        handler._admin_test_render()
        self.assertEqual(handler.response["status"], 202)
        self.assertEqual(handler.response["payload"]["status"], "queued")
        self.assertEqual(handler.response["payload"]["tile_url"], "/tile/0/0/0.png")
        self.assertEqual(store.enqueued[0][1], "diagnostic-test-render")

    def test_admin_test_render_rejects_invalid_tile(self):
        handler = _Handler(
            minimal_config(admin_enabled=True, admin_token="secret"),
            _Store(),
            token="secret",
            payload={"z": 0, "x": 2, "y": 0},
        )
        handler._admin_test_render()
        self.assertEqual(handler.response["status"], 400)

    def test_jobs_clear_requires_token(self):
        handler = _Handler(minimal_config(admin_enabled=True, admin_token="secret"), _Store())
        handler._admin_clear_jobs()
        self.assertEqual(handler.response["status"], 401)

    def test_jobs_clear_returns_counts(self):
        store = _Store()
        handler = _Handler(minimal_config(admin_enabled=True, admin_token="secret"), store, token="secret", payload={})
        handler._admin_clear_jobs()
        self.assertEqual(handler.response["payload"]["cleared"]["deleted"], 2)
        self.assertEqual(store.cleared_jobs, 1)

    def test_diagnostics_clear_requires_token(self):
        handler = _Handler(minimal_config(admin_enabled=True, admin_token="secret"), _Store())
        handler._admin_clear_diagnostics()
        self.assertEqual(handler.response["status"], 401)

    def test_diagnostics_clear_returns_counts(self):
        store = _Store()
        handler = _Handler(minimal_config(admin_enabled=True, admin_token="secret"), store, token="secret", payload={})
        handler._admin_clear_diagnostics()
        self.assertEqual(handler.response["payload"]["cleared"]["heartbeats_deleted"], 2)
        self.assertEqual(handler.response["payload"]["cleared"]["dirty_tiles_deleted"], 3)
        self.assertEqual(store.cleared_diagnostics, 1)

    def test_map_viewer_is_self_contained(self):
        handler = _Handler(minimal_config(), _Store())
        handler._map_viewer()
        self.assertIn('id="root"', handler.bytes["text"])
        self.assertIn("/static/web-ui/assets/", handler.bytes["text"])
        self.assertNotIn("leaflet", handler.bytes["text"].lower())
        self.assertNotIn("https://", handler.bytes["text"])

    def test_admin_panel_serves_static_html_without_external_assets(self):
        handler = _Handler(minimal_config(admin_enabled=True), _Store())
        handler._admin_panel()
        self.assertIn('id="root"', handler.bytes["text"])
        self.assertIn("/static/web-ui/assets/", handler.bytes["text"])
        self.assertNotIn("leaflet", handler.bytes["text"].lower())
        self.assertNotIn("https://", handler.bytes["text"])

    def test_static_assets_have_expected_content_types(self):
        admin_index = _Handler(minimal_config(), _Store())
        admin_index._static_asset("/static/web-ui/index.html")
        self.assertEqual(admin_index.bytes["content_type"], "text/html; charset=utf-8")
        self.assertIn('id="root"', admin_index.bytes["text"])

        css_asset = next(Path("tile_server/static/web-ui/assets").glob("index-*.css"))
        common = _Handler(minimal_config(), _Store())
        common._static_asset(f"/static/web-ui/assets/{css_asset.name}")
        self.assertEqual(common.bytes["content_type"], "text/css; charset=utf-8")
        self.assertIn("--otx-green", common.bytes["text"])

        asset = next(Path("tile_server/static/web-ui/assets").glob("index-*.js"))
        spa_js = _Handler(minimal_config(), _Store())
        spa_js._static_asset(f"/static/web-ui/assets/{asset.name}")
        self.assertEqual(spa_js.bytes["content_type"], "application/javascript; charset=utf-8")

    def test_static_asset_rejects_path_traversal(self):
        handler = _Handler(minimal_config(), _Store())
        handler._static_asset("/static/../api.py")
        self.assertEqual(handler.error["status"], 404)

    def test_admin_spa_source_includes_job_and_capability_endpoints(self):
        admin_jsx = Path("frontend/app/src/views/AdminView.jsx").read_text()
        self.assertIn("/admin/capabilities", admin_jsx)
        self.assertIn("/admin/jobs/import", admin_jsx)
        self.assertIn("/admin/jobs/update", admin_jsx)
        self.assertIn("/admin/jobs/reimport", admin_jsx)
        self.assertIn("/admin/jobs/expire", admin_jsx)
        self.assertIn("/admin/jobs/external-data/load", admin_jsx)
        self.assertIn("/admin/jobs/map-bundle/validate", admin_jsx)
        self.assertIn("/admin/jobs/map-bundle/activate", admin_jsx)
        self.assertIn("/admin/jobs/map-bundle/generate", admin_jsx)

    def test_admin_spa_source_contains_tabs_gating_and_overflow_controls(self):
        app_jsx = Path("frontend/app/src/views/AdminView.jsx").read_text()
        admin_css = Path("frontend/app/src/styles/app.css").read_text()
        self.assertIn("Dashboard", app_jsx)
        self.assertIn("Jobs", app_jsx)
        self.assertIn("Diagnostics", app_jsx)
        self.assertIn("External Data Policy", app_jsx)
        self.assertIn("gateForPayload", app_jsx)
        self.assertIn("exampleGate", app_jsx)
        self.assertIn("overflow-x: auto", admin_css)
        self.assertIn("workspace-shell", admin_css)
        self.assertIn("page-grid", admin_css)
        self.assertIn("toast-stack", admin_css)

    def test_admin_and_map_use_design_tokens(self):
        admin_css = Path("frontend/app/src/styles/app.css").read_text()
        map_css = Path("frontend/app/src/styles/map.css").read_text()
        self.assertIn("var(--otx-", admin_css)
        self.assertIn("var(--otx-", map_css)
        self.assertNotIn("--green:", admin_css)
        self.assertNotIn("--green:", map_css)

    def test_admin_spa_includes_local_response_panels_and_toasts(self):
        app_jsx = Path("frontend/app/src/views/AdminView.jsx").read_text()
        self.assertIn("Create Job Response", app_jsx)
        self.assertIn("Bundle Generator Response", app_jsx)
        self.assertIn("Bundle Validation Response", app_jsx)
        self.assertIn("Example Preview", app_jsx)
        self.assertIn("toast-close", Path("frontend/app/src/styles/app.css").read_text())
        self.assertIn("addToast", app_jsx)

    def test_examples_support_preview_load_and_disabled_gates(self):
        app_jsx = Path("frontend/app/src/views/AdminView.jsx").read_text()
        self.assertIn("Example Preview", app_jsx)
        self.assertIn("Preview", app_jsx)
        self.assertIn("Load into form", app_jsx)
        self.assertIn("example-badge", Path("frontend/app/src/styles/app.css").read_text())

    def test_jobs_table_uses_clickable_ids_and_copy_action(self):
        app_jsx = Path("frontend/app/src/views/AdminView.jsx").read_text()
        admin_css = Path("frontend/app/src/styles/app.css").read_text()
        self.assertIn("job-id-cell", admin_css)
        self.assertIn("openJob(job.id)", app_jsx)
        self.assertIn("copyText(job.id)", app_jsx)
        self.assertIn("CopyIcon", app_jsx)
        self.assertIn("icon-button", admin_css)

    def test_diagnostics_refresh_and_failure_limit_live_in_spa(self):
        app_jsx = Path("frontend/app/src/views/AdminView.jsx").read_text()
        self.assertIn("Render Diagnostics", app_jsx)
        self.assertIn("failures_limit", app_jsx)
        self.assertIn("document.hidden", app_jsx)
        self.assertIn("setInterval", app_jsx)
        self.assertIn("/admin/diagnostics", app_jsx)
        self.assertIn("/admin/diagnostics/test-render", app_jsx)
        self.assertIn("/admin/diagnostics/clear", app_jsx)

    def test_capability_gating_logic_lives_in_spa(self):
        app_jsx = Path("frontend/app/src/views/AdminView.jsx").read_text()
        self.assertIn("gateForPayload", app_jsx)
        self.assertIn("isRemoteUri", app_jsx)
        self.assertIn("ALLOW_INTERNAL_EXTERNAL_DATA_DOWNLOADS", Path("tile_server/api.py").read_text())
        self.assertIn("ALLOW_PUBLIC_EXTERNAL_DATA_DOWNLOADS", Path("tile_server/api.py").read_text())
        self.assertIn("fetch_external_data", app_jsx)
        self.assertIn("external_data_mode", app_jsx)

    def test_home_view_uses_brand_images_and_root_route(self):
        app_jsx = Path("frontend/app/src/App.jsx").read_text()
        home_jsx = Path("frontend/app/src/views/HomeView.jsx").read_text()
        index_html = Path("frontend/app/index.html").read_text()
        self.assertIn("return <HomeView />", app_jsx)
        self.assertIn("/static/images/OpenTilesX.svg", home_jsx)
        self.assertIn("/static/images/favicons/favicon-32x32.png", index_html)
        self.assertIn("site.webmanifest", index_html)

    def test_map_assets_include_drag_wheel_and_touch_handlers(self):
        map_jsx = Path("frontend/app/src/views/MapView.jsx").read_text()
        map_css = Path("frontend/app/src/styles/map.css").read_text()
        self.assertIn("onPointerDown", map_jsx)
        self.assertIn("onPointerMove", map_jsx)
        self.assertIn("onWheel", map_jsx)
        self.assertIn("onTouchStart", map_jsx)
        self.assertIn("onTouchMove", map_jsx)
        self.assertIn("touch-action: none", map_css)
        self.assertNotIn("event.preventDefault();\n    dragRef.current.pinchDistance", map_jsx)

    def test_map_loader_handles_queue_retry_and_cleanup(self):
        map_jsx = Path("frontend/app/src/views/MapView.jsx").read_text()
        self.assertIn("fetch(", map_jsx)
        self.assertIn("response.status === 202", map_jsx)
        self.assertIn("Retry-After", map_jsx)
        self.assertIn("URL.revokeObjectURL", map_jsx)
        self.assertIn("MAX_TILE_ATTEMPTS", map_jsx)
        self.assertIn("retryQueuedTiles", map_jsx)
        self.assertNotIn("leaflet", map_jsx.lower())

    def test_logging_and_diagnostics_hooks_are_present(self):
        api_py = Path("tile_server/api.py").read_text()
        rendering_py = Path("tile_server/rendering.py").read_text()
        cli_py = Path("tile_server/cli.py").read_text()
        jobs_py = Path("tile_server/jobs.py").read_text()
        self.assertIn("logging.basicConfig", cli_py)
        self.assertIn("tile cache miss", api_py)
        self.assertIn("tile cache hit", api_py)
        self.assertIn("render-worker starting", rendering_py)
        self.assertIn("render-worker idle", rendering_py)
        self.assertIn("render-worker leased", rendering_py)
        self.assertIn("render-worker stored", rendering_py)
        self.assertIn("render-worker failed", rendering_py)
        self.assertIn("set_render_worker_heartbeat", jobs_py)
        self.assertIn("dirty_tile_counts", jobs_py)
        self.assertIn("recent_failed_dirty_tiles", jobs_py)
        self.assertIn("render_worker_heartbeats", jobs_py)
        self.assertIn("release_dirty_tiles", jobs_py)

    def test_uploaded_bundle_can_be_validated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle_root = root / "bundle"
            (bundle_root / "style").mkdir(parents=True)
            (bundle_root / "style" / "mapnik.xml").write_text("<Map></Map>")
            (bundle_root / "manifest.json").write_text(
                json.dumps({"name": "uploaded", "version": "1", "style": {"directory": "style", "mapnikXml": "mapnik.xml"}})
            )
            archive = root / "bundle.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                for path in bundle_root.rglob("*"):
                    tar.add(path, arcname=str(path.relative_to(bundle_root)))
            data = archive.read_bytes()

        boundary = "test-boundary"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="bundle"; filename="bundle.tar.gz"\r\n'
            "Content-Type: application/gzip\r\n\r\n"
        ).encode("utf-8") + data + f"\r\n--{boundary}--\r\n".encode("utf-8")
        handler = _Handler(
            minimal_config(admin_enabled=True, admin_token="secret"),
            _Store(),
            token="secret",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "Content-Length": str(len(body))},
            body=body,
        )
        handler._admin_validate_uploaded_bundle()
        self.assertTrue(handler.response["payload"]["valid"])
        self.assertIn("checksum", handler.response["payload"])


if __name__ == "__main__":
    unittest.main()

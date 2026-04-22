import io
import tarfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from tests.helpers import minimal_config
from tile_server.rendering import HttpSidecarRenderBackend, RenderWorker
from tile_server.tiles import TileRef


class FakeStore:
    def __init__(self, leased, lock_acquired=True):
        self.leased = leased
        self.lock_acquired = lock_acquired
        self.completed = []
        self.failed = []
        self.released = []
        self.heartbeat = None
        self.settings = {}

    def lease_dirty_tiles(self, limit):
        return self.leased

    def complete_dirty_tile(self, dirty_id):
        self.completed.append(dirty_id)

    def fail_dirty_tile(self, dirty_id, error):
        self.failed.append((dirty_id, error))

    def release_dirty_tiles(self, dirty_ids, delay_seconds=1.0, reason="retry"):
        ids = list(dirty_ids)
        self.released.append((ids, delay_seconds, reason))
        return len(ids)

    @contextmanager
    def advisory_lock(self, name):
        self.lock_name = name
        yield self.lock_acquired

    def set_render_worker_heartbeat(self, worker_id, value=None):
        if value is None:
            value = worker_id
        self.heartbeat = value

    def get_setting(self, key):
        return self.settings.get(key)


class FakeStorage:
    def __init__(self, existing=None):
        self.writes = []
        self.existing = set(existing or [])

    def put(self, tile, data):
        self.writes.append((tile, data))

    def exists(self, tile):
        return tile in self.existing


class FakeRenderer:
    def __init__(self):
        self.calls = []

    def render_metatile(self, origin, size=8):
        self.calls.append((origin, size))
        return {
            TileRef(origin.layer, origin.z, origin.x + dx, origin.y + dy): b"png"
            for dx in range(size)
            for dy in range(size)
        }


class RenderWorkerTests(unittest.TestCase):
    def test_run_once_renders_and_stores_full_metatile(self):
        store = FakeStore([(101, TileRef("default", 4, 9, 10))])
        storage = FakeStorage()
        worker = RenderWorker(minimal_config(worker_id="renderer-a", metatile_size=4, store_full_metatile=True), store, storage)
        worker.renderer = FakeRenderer()

        completed = worker.run_once()

        self.assertEqual(completed, 1)
        self.assertEqual(worker.renderer.calls, [(TileRef("default", 4, 8, 8), 4)])
        self.assertEqual(len(storage.writes), 16)
        self.assertEqual(store.completed, [101])
        self.assertIn("tile_admin.metatile:default:default:4:8:8", store.lock_name)
        self.assertEqual(store.heartbeat["worker_id"], "renderer-a")
        self.assertEqual(store.heartbeat["metatile_size"], 4)
        self.assertTrue(store.heartbeat["store_full_metatile"])

    def test_run_once_can_store_only_dirty_tiles_when_disabled(self):
        dirty_tile = TileRef("default", 4, 9, 10)
        store = FakeStore([(101, dirty_tile)])
        storage = FakeStorage()
        worker = RenderWorker(minimal_config(metatile_size=4, store_full_metatile=False), store, storage)
        worker.renderer = FakeRenderer()

        worker.run_once()

        self.assertEqual(storage.writes, [(dirty_tile, b"png")])

    def test_run_once_releases_tiles_when_metatile_lock_is_busy(self):
        dirty_tile = TileRef("default", 4, 9, 10)
        store = FakeStore([(101, dirty_tile)], lock_acquired=False)
        storage = FakeStorage()
        renderer = FakeRenderer()
        worker = RenderWorker(minimal_config(metatile_size=4), store, storage)
        worker.renderer = renderer

        completed = worker.run_once()

        self.assertEqual(completed, 0)
        self.assertEqual(renderer.calls, [])
        self.assertEqual(storage.writes, [])
        self.assertEqual(store.completed, [])
        self.assertEqual(store.released[0][0], [101])
        self.assertEqual(store.released[0][2], "metatile-lock-contended")
        self.assertEqual(store.heartbeat["state"], "deferred")

    def test_run_once_completes_dirty_tile_that_is_already_cached(self):
        dirty_tile = TileRef("default", 4, 9, 10)
        store = FakeStore([(101, dirty_tile)])
        storage = FakeStorage(existing={dirty_tile})
        renderer = FakeRenderer()
        worker = RenderWorker(minimal_config(metatile_size=4), store, storage)
        worker.renderer = renderer

        completed = worker.run_once()

        self.assertEqual(completed, 1)
        self.assertEqual(renderer.calls, [])
        self.assertEqual(store.completed, [101])
        self.assertEqual(storage.writes, [])

    def test_http_sidecar_backend_reads_tile_archive(self):
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as tar:
            payload = b"png"
            info = tarfile.TarInfo(name="default/4/8/8.png")
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        body = archive.getvalue()

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self):
                return body

        backend = HttpSidecarRenderBackend(
            minimal_config(render_backend="http-sidecar", render_backend_url="http://sidecar:9000")
        )
        with mock.patch("tile_server.rendering.materialize_style_xml", return_value=Path("/tmp/mock.xml")):
            with mock.patch("pathlib.Path.read_bytes", return_value=b"<Map/>"):
                with mock.patch("tile_server.rendering.urlrequest.urlopen", return_value=Response()):
                    rendered = backend.render_metatile(TileRef("default", 4, 8, 8), size=1)

        self.assertEqual(rendered[TileRef("default", 4, 8, 8)], b"png")

    def test_worker_clears_last_error_after_diagnostics_clear_marker(self):
        store = FakeStore([])
        worker = RenderWorker(minimal_config(worker_poll_interval=0.1), store, FakeStorage())
        worker.last_error = "boom"
        worker.last_error_at = datetime.now(timezone.utc)
        store.settings["render_worker_errors_cleared_at"] = {
            "timestamp": datetime.now(timezone.utc).isoformat()
        }

        worker._heartbeat("idle", leased_count=0)

        self.assertIsNone(worker.last_error)
        self.assertIsNone(worker.last_error_at)
        self.assertIsNone(store.heartbeat["last_error"])


if __name__ == "__main__":
    unittest.main()

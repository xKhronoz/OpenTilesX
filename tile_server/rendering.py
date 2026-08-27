from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import socket
import tarfile
import tempfile
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib import request as urlrequest

from .config import AppConfig, mask_secret
from .jobs import JobStore
from .storage import TileStorage
from .style import materialize_style_xml
from .tiles import TileRef


LOGGER = logging.getLogger(__name__)
WEB_MERCATOR_HALF_WORLD = 20037508.342789244

try:
    import mapnik
except ImportError as exc:
    mapnik = None
    MAPNIK_IMPORT_ERROR = exc
else:
    MAPNIK_IMPORT_ERROR = None


@dataclass(frozen=True)
class RenderResult:
    tile: TileRef
    bytes_written: int


class RenderBackend:
    def prepare_layer(self, layer: str) -> None:
        return

    def render_tile(self, tile: TileRef) -> bytes:
        return self.render_metatile(tile, size=1)[tile]

    def render_metatile(self, origin: TileRef, size: int = 8) -> dict[TileRef, bytes]:
        raise NotImplementedError


class PythonMapnikRenderBackend(RenderBackend):
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self._style_cache: dict[str, Path] = {}

    def prepare_layer(self, layer: str) -> None:
        self._style_xml(layer)

    def render_tile(self, tile: TileRef) -> bytes:
        mapnik_module = _require_mapnik()
        size = 256
        world = mapnik_module.Map(size, size)
        mapnik_module.load_map(world, str(self._style_xml(tile.layer)))
        world.zoom_to_box(_tile_box(tile, mapnik_module))
        image = mapnik_module.Image(size, size)
        mapnik_module.render(world, image)
        return _image_to_png_bytes(image)

    def render_metatile(self, origin: TileRef, size: int = 8) -> dict[TileRef, bytes]:
        mapnik_module = _require_mapnik()
        rendered = {}
        max_coord = 1 << origin.z
        tiles_x = min(size, max_coord - origin.x)
        tiles_y = min(size, max_coord - origin.y)
        if tiles_x <= 0 or tiles_y <= 0:
            return rendered

        tile_size = 256
        world = mapnik_module.Map(tile_size * tiles_x, tile_size * tiles_y)
        mapnik_module.load_map(world, str(self._style_xml(origin.layer)))
        world.zoom_to_box(_metatile_box(origin, tiles_x, tiles_y, mapnik_module))
        image = mapnik_module.Image(tile_size * tiles_x, tile_size * tiles_y)
        mapnik_module.render(world, image)

        for dx in range(tiles_x):
            for dy in range(tiles_y):
                tile = TileRef(origin.layer, origin.z, origin.x + dx, origin.y + dy)
                view = image.view(dx * tile_size, dy * tile_size, tile_size, tile_size)
                rendered[tile] = _image_to_png_bytes(view)
        return rendered

    def _style_xml(self, layer: str) -> Path:
        if layer not in self._style_cache:
            self._style_cache[layer] = materialize_style_xml(self.config, layer=layer)
        return self._style_cache[layer]


class HttpSidecarRenderBackend(RenderBackend):
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.base_url = (config.render_backend_url or "").rstrip("/")
        self._style_cache: dict[str, Path] = {}
        self._style_checksums: dict[str, str] = {}

    def prepare_layer(self, layer: str) -> None:
        self._style_xml(layer)
        self._style_checksum(layer)

    def render_metatile(self, origin: TileRef, size: int = 8) -> dict[TileRef, bytes]:
        payload = {
            "layer": origin.layer,
            "z": origin.z,
            "x": origin.x,
            "y": origin.y,
            "metatile_size": size,
            "map_version": self.config.map_version,
            "style_checksum": self._style_checksum(origin.layer),
        }
        body = json.dumps(payload).encode("utf-8")
        req = urlrequest.Request(
            self.base_url + "/render/metatile",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlrequest.urlopen(req, timeout=120) as response:
            data = response.read()
        return _parse_metatile_archive(origin.layer, data)

    def _style_xml(self, layer: str) -> Path:
        if layer not in self._style_cache:
            self._style_cache[layer] = materialize_style_xml(self.config, layer=layer)
        return self._style_cache[layer]

    def _style_checksum(self, layer: str) -> str:
        if layer not in self._style_checksums:
            path = self._style_xml(layer)
            self._style_checksums[layer] = hashlib.sha256(path.read_bytes()).hexdigest()
        return self._style_checksums[layer]


def build_render_backend(config: AppConfig) -> RenderBackend:
    if config.render_backend == "python-mapnik":
        return PythonMapnikRenderBackend(config)
    if config.render_backend == "http-sidecar":
        return HttpSidecarRenderBackend(config)
    raise RenderError(f"Unsupported render backend: {config.render_backend}")


def _require_mapnik() -> object:
    if mapnik is None:
        raise RenderError("python3-mapnik is required for render-worker") from MAPNIK_IMPORT_ERROR
    return mapnik


class RenderWorker:
    def __init__(self, config: AppConfig, store: JobStore, storage: TileStorage) -> None:
        self.config = config
        self.store = store
        self.storage = storage
        self.renderer = build_render_backend(config)
        self.worker_id = config.worker_id or f"{socket.gethostname()}-{os.getpid()}"
        self.processed_count = 0
        self.last_error: str | None = None
        self.last_error_at: datetime | None = None
        self.last_render_seconds = 0.0
        self.last_store_seconds = 0.0
        self.last_queue_age_seconds = 0.0
        self.last_render_slot_wait_seconds = 0.0
        self.render_slot_wait_seconds_total = 0.0
        self.render_slot_busy_count = 0
        self.render_slot_acquired_count = 0
        self.render_timeout_count = 0
        self.render_failure_count = 0
        self.metatile_lock_contention_count = 0
        self.storage_write_count = 0
        self._recent_failures: deque[bool] = deque(maxlen=5)
        self._idle_logged_at = 0.0
        self._last_error_clear_check = 0.0

    def run_once(self) -> int:
        leased = self.store.lease_dirty_tiles(self.config.worker_batch_size)
        if not leased:
            self._heartbeat("idle", leased_count=0)
            self._log_idle()
            return 0

        LOGGER.info("render-worker leased dirty_tiles=%s", len(leased))
        completed = 0
        deferred = 0
        for job in leased:
            dirty_ids = [job.id]
            origin = job.origin
            self.last_queue_age_seconds = _queue_age_seconds(job.requested_at)
            if self._should_back_off():
                released = self.store.release_dirty_tiles(
                    dirty_ids,
                    delay_seconds=max(2.0, self.config.worker_poll_interval * 2),
                    reason="render-backpressure",
                )
                deferred += released
                continue
            try:
                slot_started = time.monotonic()
                with self.store.advisory_lock_first(_render_db_slot_names(self.config.map_version, self.config.render_db_max_active)) as slot_name:
                    self.last_render_slot_wait_seconds = time.monotonic() - slot_started
                    self.render_slot_wait_seconds_total += self.last_render_slot_wait_seconds
                    if not slot_name:
                        self.render_slot_busy_count += 1
                        released = self.store.release_dirty_tiles(
                            dirty_ids,
                            delay_seconds=max(1.0, self.config.worker_poll_interval),
                            reason="render-db-slot-busy",
                        )
                        deferred += released
                        continue
                    self.render_slot_acquired_count += 1
                    with self.store.advisory_lock(_metatile_lock_name(self.config.map_version, origin)) as locked:
                        if not locked:
                            self.metatile_lock_contention_count += 1
                            released = self.store.release_dirty_tiles(
                                dirty_ids,
                                delay_seconds=max(1.0, self.config.worker_poll_interval),
                                reason="metatile-lock-contended",
                            )
                            deferred += released
                            LOGGER.info(
                                "render-worker deferred metatile lock busy layer=%s z=%s x=%s y=%s dirty_tiles=%s released=%s",
                                origin.layer,
                                origin.z,
                                origin.x,
                                origin.y,
                                len(dirty_ids),
                                released,
                            )
                            continue
                        LOGGER.info(
                            "render-worker rendering metatile layer=%s z=%s x=%s y=%s dirty_tiles=%s slot=%s queue_age=%ss",
                            origin.layer,
                            origin.z,
                            origin.x,
                            origin.y,
                            len(dirty_ids),
                            slot_name,
                            round(self.last_queue_age_seconds, 3),
                        )
                        self._heartbeat("rendering", leased_count=len(leased))
                        render_started = time.monotonic()
                        rendered = self.renderer.render_metatile(origin, size=job.metatile_size)
                        self.last_render_seconds = time.monotonic() - render_started
                        tiles_to_store = list(rendered.items()) if self.config.store_full_metatile else [
                            (job.tile, rendered[job.tile])
                        ]
                        self._heartbeat("storing", leased_count=len(leased))
                        store_started = time.monotonic()
                        stored = self.storage.put_many(tiles_to_store)
                        self.last_store_seconds = time.monotonic() - store_started
                        self.storage_write_count += stored
                        for dirty_id in dirty_ids:
                            self.store.complete_dirty_tile(dirty_id)
                            completed += 1
                        self.processed_count += len(dirty_ids)
                        self.last_error = None
                        self.last_error_at = None
                        self._recent_failures.append(False)
                        LOGGER.info(
                            "render-worker stored tiles=%s completed_dirty_tiles=%s metatile=%s/%s/%s/%s total_processed=%s",
                            stored,
                            len(dirty_ids),
                            origin.layer,
                            origin.z,
                            origin.x,
                            origin.y,
                            self.processed_count,
                        )
            except Exception as exc:
                self.last_error = str(exc)
                self.last_error_at = datetime.now(timezone.utc)
                self.render_failure_count += 1
                if "statement timeout" in str(exc).lower():
                    self.render_timeout_count += 1
                self._recent_failures.append(True)
                LOGGER.exception(
                    "render-worker failed metatile layer=%s z=%s x=%s y=%s dirty_tiles=%s",
                    origin.layer,
                    origin.z,
                    origin.x,
                    origin.y,
                    len(dirty_ids),
                )
                for dirty_id in dirty_ids:
                    self.store.fail_dirty_tile(dirty_id, str(exc))
        state = "processed" if completed else "deferred" if deferred else "failed"
        self._heartbeat(state, leased_count=len(leased))
        return completed

    def run_forever(self) -> None:
        LOGGER.info(
            "render-worker starting worker_id=%s backend=%s tile_store=%s map_version=%s batch_size=%s poll_interval=%s render_database=%s metadata_database=%s",
            self.worker_id,
            self.config.render_backend,
            self.config.tile_store,
            self.config.map_version,
            self.config.worker_batch_size,
            self.config.worker_poll_interval,
            mask_secret(self.config.render_database_url),
            mask_secret(self.config.job_database_url),
        )
        try:
            self.renderer.prepare_layer(self.config.default_layer)
        except Exception:
            LOGGER.exception("render-worker style prewarm failed layer=%s", self.config.default_layer)
        self._heartbeat("starting", leased_count=0)
        while True:
            processed = self.run_once()
            if not processed:
                time.sleep(self.config.worker_poll_interval)

    def _heartbeat(self, state: str, leased_count: int) -> None:
        try:
            self._maybe_clear_last_error()
            self.store.set_render_worker_heartbeat(
                self.worker_id,
                {
                    "worker_id": self.worker_id,
                    "hostname": socket.gethostname(),
                    "pid": os.getpid(),
                    "role": "render-worker",
                    "last_seen": datetime.now(timezone.utc).isoformat(),
                    "state": state,
                    "leased_count": leased_count,
                    "processed_count": self.processed_count,
                    "last_error": self.last_error,
                    "tile_store": self.config.tile_store,
                    "map_version": self.config.map_version,
                    "render_backend": self.config.render_backend,
                    "render_worker_processes": self.config.render_worker_processes,
                    "metatile_size": self.config.metatile_size,
                    "store_full_metatile": self.config.store_full_metatile,
                    "last_render_seconds": round(self.last_render_seconds, 4),
                    "last_store_seconds": round(self.last_store_seconds, 4),
                    "last_queue_age_seconds": round(self.last_queue_age_seconds, 4),
                    "render_db_max_active": self.config.render_db_max_active,
                    "render_db_statement_timeout_ms": self.config.render_db_statement_timeout_ms,
                    "last_render_slot_wait_seconds": round(self.last_render_slot_wait_seconds, 4),
                    "render_slot_wait_seconds_total": round(self.render_slot_wait_seconds_total, 4),
                    "render_slot_busy_count": self.render_slot_busy_count,
                    "render_slot_acquired_count": self.render_slot_acquired_count,
                    "render_timeout_count": self.render_timeout_count,
                    "render_failure_count": self.render_failure_count,
                    "metatile_lock_contention_count": self.metatile_lock_contention_count,
                    "storage_write_count": self.storage_write_count,
                }
            )
        except Exception:
            LOGGER.exception("render-worker heartbeat update failed")

    def _log_idle(self) -> None:
        now = time.monotonic()
        if now - self._idle_logged_at >= max(30, self.config.worker_poll_interval * 3):
            LOGGER.info("render-worker idle no dirty tiles leased")
            self._idle_logged_at = now

    def _maybe_clear_last_error(self) -> None:
        if not self.last_error:
            return
        now = time.monotonic()
        if now - self._last_error_clear_check < max(1.0, self.config.worker_poll_interval):
            return
        self._last_error_clear_check = now
        if not hasattr(self.store, "get_setting"):
            return
        cleared = self.store.get_setting("render_worker_errors_cleared_at")
        if not isinstance(cleared, dict):
            return
        timestamp = _parse_timestamp(cleared.get("timestamp"))
        if not timestamp:
            return
        if self.last_error_at is None or timestamp >= self.last_error_at:
            self.last_error = None
            self.last_error_at = None

    def _should_back_off(self) -> bool:
        return len(self._recent_failures) >= 3 and sum(1 for failed in self._recent_failures if failed) >= 3


def _tile_box(tile: TileRef, mapnik_module: object) -> object:
    tiles = 1 << tile.z
    minx = tile.x / tiles * 2 * WEB_MERCATOR_HALF_WORLD - WEB_MERCATOR_HALF_WORLD
    maxx = (tile.x + 1) / tiles * 2 * WEB_MERCATOR_HALF_WORLD - WEB_MERCATOR_HALF_WORLD
    maxy = WEB_MERCATOR_HALF_WORLD - tile.y / tiles * 2 * WEB_MERCATOR_HALF_WORLD
    miny = WEB_MERCATOR_HALF_WORLD - (tile.y + 1) / tiles * 2 * WEB_MERCATOR_HALF_WORLD
    return mapnik_module.Box2d(minx, miny, maxx, maxy)


def _metatile_box(origin: TileRef, tiles_x: int, tiles_y: int, mapnik_module: object) -> object:
    tiles = 1 << origin.z
    minx = origin.x / tiles * 2 * WEB_MERCATOR_HALF_WORLD - WEB_MERCATOR_HALF_WORLD
    maxx = (origin.x + tiles_x) / tiles * 2 * WEB_MERCATOR_HALF_WORLD - WEB_MERCATOR_HALF_WORLD
    maxy = WEB_MERCATOR_HALF_WORLD - origin.y / tiles * 2 * WEB_MERCATOR_HALF_WORLD
    miny = WEB_MERCATOR_HALF_WORLD - (origin.y + tiles_y) / tiles * 2 * WEB_MERCATOR_HALF_WORLD
    return mapnik_module.Box2d(minx, miny, maxx, maxy)


def _metatile_lock_name(map_version: str, origin: TileRef) -> str:
    return f"tile_admin.metatile:{map_version}:{origin.layer}:{origin.z}:{origin.x}:{origin.y}"


def _render_db_slot_names(map_version: str, max_active: int) -> list[str]:
    return [f"tile_admin.renderdb:{map_version}:{slot}" for slot in range(max(1, max_active))]


def _queue_age_seconds(requested_at: datetime | None) -> float:
    if not requested_at:
        return 0.0
    return max(0.0, (datetime.now(timezone.utc) - requested_at.astimezone(timezone.utc)).total_seconds())


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _image_to_png_bytes(image: object) -> bytes:
    try:
        data = image.tostring("png")
        if isinstance(data, str):
            data = data.encode("latin1")
        return data
    except Exception:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
            name = handle.name
        try:
            image.save(name, "png")
            return Path(name).read_bytes()
        finally:
            Path(name).unlink(missing_ok=True)


def _parse_metatile_archive(default_layer: str, payload: bytes) -> dict[TileRef, bytes]:
    rendered: dict[TileRef, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:*") as archive:
        for member in archive.getmembers():
            if not member.isfile() or not member.name.endswith(".png"):
                continue
            tile = _tile_from_archive_name(default_layer, member.name)
            handle = archive.extractfile(member)
            if not handle:
                continue
            rendered[tile] = handle.read()
    if not rendered:
        raise RenderError("render sidecar returned no PNG tiles")
    return rendered


def _tile_from_archive_name(default_layer: str, name: str) -> TileRef:
    parts = [part for part in Path(name).parts if part]
    if len(parts) >= 4 and parts[-3].isdigit() and parts[-2].isdigit():
        layer = parts[-4]
        z, x, y_name = parts[-3], parts[-2], parts[-1]
    elif len(parts) >= 3 and parts[-3].isdigit() and parts[-2].isdigit():
        layer = default_layer
        z, x, y_name = parts[-3], parts[-2], parts[-1]
    else:
        raise RenderError(f"Unexpected tile path in render archive: {name}")
    return TileRef(layer, int(z), int(x), int(Path(y_name).stem))


class RenderError(RuntimeError):
    pass

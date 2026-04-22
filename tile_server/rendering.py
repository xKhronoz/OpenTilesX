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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib import request as urlrequest

from .config import AppConfig, mask_secret
from .jobs import JobStore
from .storage import TileStorage
from .style import materialize_style_xml
from .tiles import TileRef, metatile_origin


LOGGER = logging.getLogger(__name__)
WEB_MERCATOR_HALF_WORLD = 20037508.342789244


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
        try:
            import mapnik
        except ImportError as exc:
            raise RenderError("python3-mapnik is required for render-worker") from exc

        size = 256
        world = mapnik.Map(size, size)
        mapnik.load_map(world, str(self._style_xml(tile.layer)))
        world.zoom_to_box(_tile_box(tile, mapnik))
        image = mapnik.Image(size, size)
        mapnik.render(world, image)
        return _image_to_png_bytes(image)

    def render_metatile(self, origin: TileRef, size: int = 8) -> dict[TileRef, bytes]:
        try:
            import mapnik
        except ImportError as exc:
            raise RenderError("python3-mapnik is required for render-worker") from exc

        rendered = {}
        max_coord = 1 << origin.z
        tiles_x = min(size, max_coord - origin.x)
        tiles_y = min(size, max_coord - origin.y)
        if tiles_x <= 0 or tiles_y <= 0:
            return rendered

        tile_size = 256
        world = mapnik.Map(tile_size * tiles_x, tile_size * tiles_y)
        mapnik.load_map(world, str(self._style_xml(origin.layer)))
        world.zoom_to_box(_metatile_box(origin, tiles_x, tiles_y, mapnik))
        image = mapnik.Image(tile_size * tiles_x, tile_size * tiles_y)
        mapnik.render(world, image)

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
        by_origin: dict[TileRef, list[tuple[int, TileRef]]] = {}
        for dirty_id, tile in leased:
            if self.config.tile_store == "filesystem":
                try:
                    if self.storage.exists(tile):
                        self.store.complete_dirty_tile(dirty_id)
                        completed += 1
                        LOGGER.debug("render-worker completed already cached tile layer=%s z=%s x=%s y=%s", tile.layer, tile.z, tile.x, tile.y)
                        continue
                except Exception:
                    LOGGER.exception("render-worker cache existence check failed layer=%s z=%s x=%s y=%s", tile.layer, tile.z, tile.x, tile.y)
            by_origin.setdefault(metatile_origin(tile, self.config.metatile_size), []).append((dirty_id, tile))

        deferred = 0
        for origin, dirty_tiles in by_origin.items():
            dirty_ids = [dirty_id for dirty_id, _tile in dirty_tiles]
            try:
                with self.store.advisory_lock(_metatile_lock_name(self.config.map_version, origin)) as locked:
                    if not locked:
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
                            len(dirty_tiles),
                            released,
                        )
                        continue
                    LOGGER.info(
                        "render-worker rendering metatile layer=%s z=%s x=%s y=%s dirty_tiles=%s",
                        origin.layer,
                        origin.z,
                        origin.x,
                        origin.y,
                        len(dirty_tiles),
                    )
                    render_started = time.monotonic()
                    rendered = self.renderer.render_metatile(origin, size=self.config.metatile_size)
                    self.last_render_seconds = time.monotonic() - render_started
                    tiles_to_store = rendered.items() if self.config.store_full_metatile else (
                        (tile, rendered[tile]) for _, tile in dirty_tiles
                    )
                    stored = 0
                    store_started = time.monotonic()
                    for tile, data in tiles_to_store:
                        self.storage.put(tile, data)
                        stored += 1
                    self.last_store_seconds = time.monotonic() - store_started
                    for dirty_id, _tile in dirty_tiles:
                        self.store.complete_dirty_tile(dirty_id)
                        completed += 1
                    self.processed_count += len(dirty_tiles)
                    self.last_error = None
                    self.last_error_at = None
                    LOGGER.info(
                        "render-worker stored tiles=%s completed_dirty_tiles=%s metatile=%s/%s/%s/%s total_processed=%s",
                        stored,
                        len(dirty_tiles),
                        origin.layer,
                        origin.z,
                        origin.x,
                        origin.y,
                        self.processed_count,
                    )
            except Exception as exc:
                self.last_error = str(exc)
                self.last_error_at = datetime.now(timezone.utc)
                LOGGER.exception(
                    "render-worker failed metatile layer=%s z=%s x=%s y=%s dirty_tiles=%s",
                    origin.layer,
                    origin.z,
                    origin.x,
                    origin.y,
                    len(dirty_tiles),
                )
                for dirty_id, _tile in dirty_tiles:
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

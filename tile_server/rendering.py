from __future__ import annotations

import math
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .config import AppConfig
from .jobs import JobStore
from .storage import TileStorage
from .style import materialize_style_xml
from .tiles import TileRef, metatile_origin


WEB_MERCATOR_HALF_WORLD = 20037508.342789244


@dataclass(frozen=True)
class RenderResult:
    tile: TileRef
    bytes_written: int


class TileRenderer:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self._style_cache: dict[str, Path] = {}

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

    def render_metatile(self, origin: TileRef, size: int = 8) -> dict[TileRef, bytes]:
        rendered = {}
        max_coord = 1 << origin.z
        for dx in range(size):
            for dy in range(size):
                x = origin.x + dx
                y = origin.y + dy
                if x < max_coord and y < max_coord:
                    tile = TileRef(origin.layer, origin.z, x, y)
                    rendered[tile] = self.render_tile(tile)
        return rendered

    def _style_xml(self, layer: str) -> Path:
        if layer not in self._style_cache:
            self._style_cache[layer] = materialize_style_xml(self.config, layer=layer)
        return self._style_cache[layer]


class RenderWorker:
    def __init__(self, config: AppConfig, store: JobStore, storage: TileStorage) -> None:
        self.config = config
        self.store = store
        self.storage = storage
        self.renderer = TileRenderer(config)

    def run_once(self) -> int:
        leased = self.store.lease_dirty_tiles(self.config.worker_batch_size)
        if not leased:
            return 0

        by_origin: dict[TileRef, list[tuple[int, TileRef]]] = {}
        for dirty_id, tile in leased:
            by_origin.setdefault(metatile_origin(tile), []).append((dirty_id, tile))

        completed = 0
        for origin, dirty_tiles in by_origin.items():
            try:
                rendered = self.renderer.render_metatile(origin)
                for dirty_id, tile in dirty_tiles:
                    data = rendered[tile]
                    self.storage.put(tile, data)
                    self.store.complete_dirty_tile(dirty_id)
                    completed += 1
            except Exception as exc:
                for dirty_id, _tile in dirty_tiles:
                    self.store.fail_dirty_tile(dirty_id, str(exc))
        return completed

    def run_forever(self) -> None:
        while True:
            processed = self.run_once()
            if not processed:
                time.sleep(self.config.worker_poll_interval)


def _tile_box(tile: TileRef, mapnik_module: object) -> object:
    tiles = 1 << tile.z
    minx = tile.x / tiles * 2 * WEB_MERCATOR_HALF_WORLD - WEB_MERCATOR_HALF_WORLD
    maxx = (tile.x + 1) / tiles * 2 * WEB_MERCATOR_HALF_WORLD - WEB_MERCATOR_HALF_WORLD
    maxy = WEB_MERCATOR_HALF_WORLD - tile.y / tiles * 2 * WEB_MERCATOR_HALF_WORLD
    miny = WEB_MERCATOR_HALF_WORLD - (tile.y + 1) / tiles * 2 * WEB_MERCATOR_HALF_WORLD
    return mapnik_module.Box2d(minx, miny, maxx, maxy)


class RenderError(RuntimeError):
    pass

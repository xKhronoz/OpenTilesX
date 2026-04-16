from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, order=True)
class TileRef:
    layer: str
    z: int
    x: int
    y: int

    @classmethod
    def from_segments(cls, default_layer: str, segments: list[str]) -> "TileRef":
        if len(segments) == 3:
            layer = default_layer
            z_s, x_s, y_s = segments
        elif len(segments) == 4:
            layer, z_s, x_s, y_s = segments
        else:
            raise TileError("Tile path must be /tile/{z}/{x}/{y}.png or /tile/{layer}/{z}/{x}/{y}.png")

        if not y_s.endswith(".png"):
            raise TileError("Only .png tiles are supported")

        try:
            z = int(z_s)
            x = int(x_s)
            y = int(y_s[:-4])
        except ValueError as exc:
            raise TileError("Tile coordinates must be integers") from exc

        tile = cls(layer=layer, z=z, x=x, y=y)
        tile.validate()
        return tile

    def validate(self) -> None:
        if not self.layer or "/" in self.layer or ".." in self.layer:
            raise TileError("Invalid tile layer")
        if self.z < 0 or self.z > 30:
            raise TileError("Tile z must be between 0 and 30")
        max_coord = 1 << self.z
        if self.x < 0 or self.y < 0 or self.x >= max_coord or self.y >= max_coord:
            raise TileError("Tile x/y is out of range for z")


class TileError(ValueError):
    pass


def tile_key(prefix: str, map_version: str, tile: TileRef) -> str:
    parts = [prefix.strip("/"), map_version.strip("/"), tile.layer.strip("/"), str(tile.z), str(tile.x), f"{tile.y}.png"]
    return "/".join(part for part in parts if part)


def metatile_origin(tile: TileRef, size: int = 8) -> TileRef:
    return TileRef(layer=tile.layer, z=tile.z, x=(tile.x // size) * size, y=(tile.y // size) * size)

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .config import AppConfig
from .tiles import TileRef, tile_key

try:
    import boto3
    from botocore.config import Config as BotocoreConfig
    from botocore.exceptions import ClientError as S3ClientError
except ImportError as exc:
    boto3 = None
    BotocoreConfig = None
    S3ClientError = None
    BOTO3_IMPORT_ERROR = exc
else:
    BOTO3_IMPORT_ERROR = None


@dataclass(frozen=True)
class TileObject:
    data: bytes
    content_type: str = "image/png"
    etag: Optional[str] = None


class TileStorage:
    def get(self, tile: TileRef) -> Optional[TileObject]:
        raise NotImplementedError

    def put(self, tile: TileRef, data: bytes, content_type: str = "image/png") -> None:
        raise NotImplementedError

    def put_many(self, tiles: list[tuple[TileRef, bytes]], content_type: str = "image/png") -> int:
        for tile, data in tiles:
            self.put(tile, data, content_type=content_type)
        return len(tiles)

    def delete(self, tile: TileRef) -> None:
        raise NotImplementedError

    def exists(self, tile: TileRef) -> bool:
        return self.get(tile) is not None


class FilesystemTileStorage(TileStorage):
    def __init__(self, root: str, map_version: str, prefix: str = "") -> None:
        self.root = Path(root)
        self.map_version = map_version
        self.prefix = prefix

    def _path(self, tile: TileRef) -> Path:
        key = tile_key(self.prefix, self.map_version, tile)
        path = (self.root / key).resolve()
        root = self.root.resolve()
        if root not in path.parents and path != root:
            raise StorageError("Tile path escapes TILE_FS_PATH")
        return path

    def get(self, tile: TileRef) -> Optional[TileObject]:
        path = self._path(tile)
        if not path.is_file():
            return None
        return TileObject(path.read_bytes())

    def put(self, tile: TileRef, data: bytes, content_type: str = "image/png") -> None:
        path = self._path(tile)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".tile-", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "wb") as tmp:
                tmp.write(data)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_name, path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def put_many(self, tiles: list[tuple[TileRef, bytes]], content_type: str = "image/png") -> int:
        prepared: list[tuple[str, Path]] = []
        try:
            for tile, data in tiles:
                path = self._path(tile)
                path.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp_name = tempfile.mkstemp(prefix=".tile-", suffix=".tmp", dir=str(path.parent))
                with os.fdopen(fd, "wb") as tmp:
                    tmp.write(data)
                prepared.append((tmp_name, path))
            for tmp_name, path in prepared:
                os.replace(tmp_name, path)
            return len(prepared)
        finally:
            for tmp_name, _path in prepared:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)

    def delete(self, tile: TileRef) -> None:
        path = self._path(tile)
        try:
            path.unlink()
        except FileNotFoundError:
            return


class S3TileStorage(TileStorage):
    def __init__(self, config: AppConfig) -> None:
        if not config.s3:
            raise StorageError("S3 configuration is missing")
        if boto3 is None or BotocoreConfig is None or S3ClientError is None:
            raise StorageError("boto3 is required when TILE_STORE=s3") from BOTO3_IMPORT_ERROR

        self._client_error = S3ClientError
        self.bucket = config.s3.bucket
        self.prefix = config.s3.prefix
        self.map_version = config.map_version

        s3_options = {}
        if config.s3.force_path_style:
            s3_options["s3"] = {"addressing_style": "path"}
        client_config = BotocoreConfig(signature_version="s3v4", **s3_options)
        self.client = boto3.client(
            "s3",
            region_name=config.s3.region,
            endpoint_url=config.s3.endpoint_url,
            aws_access_key_id=config.s3.access_key_id,
            aws_secret_access_key=config.s3.secret_access_key,
            config=client_config,
        )

    def _key(self, tile: TileRef) -> str:
        return tile_key(self.prefix, self.map_version, tile)

    def get(self, tile: TileRef) -> Optional[TileObject]:
        try:
            obj = self.client.get_object(Bucket=self.bucket, Key=self._key(tile))
        except self._client_error as exc:
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            code = exc.response.get("Error", {}).get("Code")
            if status == 404 or code in {"NoSuchKey", "404"}:
                return None
            raise
        return TileObject(
            data=obj["Body"].read(),
            content_type=obj.get("ContentType", "image/png"),
            etag=obj.get("ETag"),
        )

    def put(self, tile: TileRef, data: bytes, content_type: str = "image/png") -> None:
        self.client.put_object(
            Bucket=self.bucket,
            Key=self._key(tile),
            Body=data,
            ContentType=content_type,
            CacheControl="public, max-age=3600",
        )

    def put_many(self, tiles: list[tuple[TileRef, bytes]], content_type: str = "image/png") -> int:
        for tile, data in tiles:
            self.put(tile, data, content_type=content_type)
        return len(tiles)

    def delete(self, tile: TileRef) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=self._key(tile))


class StorageError(RuntimeError):
    pass


def build_storage(config: AppConfig) -> TileStorage:
    if config.tile_store == "filesystem":
        return FilesystemTileStorage(config.tile_fs_path, config.map_version)
    if config.tile_store == "s3":
        return S3TileStorage(config)
    raise StorageError(f"Unsupported TILE_STORE: {config.tile_store}")

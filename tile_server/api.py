from __future__ import annotations

import json
import logging
import tempfile
import uuid
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .bundles import MapBundleValidator, bundle_checksum
from .config import AppConfig, mask_secret
from .external_data import ExternalDataManager
from .jobs import JobStore
from .storage import TileStorage
from .tiles import TileError, TileRef


LOGGER = logging.getLogger(__name__)

JOB_ENDPOINTS = {
    "/admin/jobs/import": "import",
    "/admin/jobs/update": "update",
    "/admin/jobs/reimport": "reimport",
    "/admin/jobs/expire": "expire",
    "/admin/jobs/external-data/load": "external_data_load",
    "/admin/jobs/map-bundle/validate": "bundle_validate",
    "/admin/jobs/map-bundle/activate": "bundle_activate",
    "/admin/jobs/map-bundle/generate": "bundle_generate",
}

MAX_BUNDLE_UPLOAD_BYTES = 512 * 1024 * 1024
STATIC_DIR = Path(__file__).with_name("static")
STATIC_CONTENT_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".ico": "image/x-icon",
    ".js": "application/javascript; charset=utf-8",
    ".map": "application/json",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".webmanifest": "application/manifest+json",
}


class TileApiHandler(BaseHTTPRequestHandler):
    config: AppConfig
    storage: TileStorage
    store: JobStore

    server_version = "OpenTilesX/0.1"

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self._home_panel()
            return
        if path == "/healthz":
            self._json(
                {
                    "status": "ok",
                    "role": self.config.role,
                    "tile_store": self.config.tile_store,
                    "admin_enabled": self.config.admin_enabled,
                }
            )
            return
        if path == "/metrics":
            self._text("opentilesx_up 1\n", content_type="text/plain; version=0.0.4")
            return
        if path == "/admin":
            self._admin_panel()
            return
        if path == "/map":
            self._map_viewer()
            return
        if path.startswith("/static/"):
            self._static_asset(path)
            return
        if path.startswith("/tile/"):
            self._serve_tile(path)
            return
        if path == "/admin/diagnostics":
            self._admin_diagnostics()
            return
        if path == "/admin/capabilities":
            self._admin_capabilities()
            return
        if path == "/admin/jobs":
            self._admin_list_jobs()
            return
        if path.startswith("/admin/jobs/"):
            self._admin_get_job(path)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/admin/diagnostics/test-render":
            self._admin_test_render()
            return
        if path == "/admin/diagnostics/clear":
            self._admin_clear_diagnostics()
            return
        if path == "/admin/jobs/clear":
            self._admin_clear_jobs()
            return
        if path in JOB_ENDPOINTS:
            self._admin_create_job(path)
            return
        if path == "/admin/map-bundle/validate-upload":
            self._admin_validate_uploaded_bundle()
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
            LOGGER.debug("tile cache hit layer=%s z=%s x=%s y=%s", tile.layer, tile.z, tile.x, tile.y)
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", obj.content_type)
            self.send_header("Cache-Control", "public, max-age=3600")
            self.send_header("Content-Length", str(len(obj.data)))
            self.end_headers()
            self.wfile.write(obj.data)
            return

        try:
            inserted = self.store.enqueue_dirty_tile(tile)
            LOGGER.info(
                "tile cache miss enqueued=%s layer=%s z=%s x=%s y=%s",
                inserted,
                tile.layer,
                tile.z,
                tile.x,
                tile.y,
            )
        except Exception as exc:
            LOGGER.exception("tile cache miss enqueue failed layer=%s z=%s x=%s y=%s", tile.layer, tile.z, tile.x, tile.y)
            self._json({"error": f"Tile missing and render enqueue failed: {exc}"}, status=HTTPStatus.SERVICE_UNAVAILABLE)
            return

        self._json({"status": "queued", "tile": tile.__dict__}, status=HTTPStatus.ACCEPTED, headers={"Retry-After": "3"})

    def _admin_diagnostics(self) -> None:
        if not self._authorized():
            return
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        try:
            failures_limit = max(1, min(int(params.get("failures_limit", ["20"])[0]), 100))
        except ValueError:
            failures_limit = 20
        try:
            if hasattr(self.store, "render_worker_heartbeats"):
                heartbeats = self.store.render_worker_heartbeats()
            else:
                heartbeat = self.store.get_setting("render_worker_heartbeat")
                heartbeats = [heartbeat] if heartbeat else []
            dirty_tiles = self.store.dirty_tile_counts()
            failures = self.store.recent_failed_dirty_tiles(limit=failures_limit)
        except Exception as exc:
            self._json({"error": str(exc)}, status=HTTPStatus.SERVICE_UNAVAILABLE)
            return
        workers = [_render_worker_health(heartbeat, self.config.worker_poll_interval) for heartbeat in heartbeats]
        capabilities = _admin_capabilities_payload(self.config)
        self._json(
            {
                "render_worker": _render_worker_summary(workers, self.config.worker_poll_interval),
                "render_workers": workers,
                "rendering": _rendering_summary(self.config),
                "dirty_tiles": dirty_tiles,
                "recent_failures": failures,
                "recent_failures_limit": failures_limit,
                "storage": _storage_summary(self.config),
                "database": _database_summary(self.config),
                "external_data": ExternalDataManager(self.config).summary(_style_dir_for_summary(self.config)),
                "capabilities": capabilities,
            }
        )

    def _admin_capabilities(self) -> None:
        if not self._authorized():
            return
        self._json(_admin_capabilities_payload(self.config))

    def _admin_test_render(self) -> None:
        if not self._authorized():
            return
        try:
            payload = self._read_json()
            tile = TileRef(
                layer=payload.get("layer") or self.config.default_layer,
                z=int(payload.get("z", 0)),
                x=int(payload.get("x", 0)),
                y=int(payload.get("y", 0)),
            )
            tile.validate()
        except Exception as exc:
            self._json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        try:
            exists = self.storage.exists(tile)
            enqueued = False if exists else self.store.enqueue_dirty_tile(tile, reason="diagnostic-test-render")
        except Exception as exc:
            self._json({"error": str(exc)}, status=HTTPStatus.SERVICE_UNAVAILABLE)
            return
        self._json(
            {
                "status": "exists" if exists else "queued",
                "exists": exists,
                "enqueued": enqueued,
                "tile": tile.__dict__,
                "tile_url": _tile_url(tile, self.config.default_layer),
            },
            status=HTTPStatus.OK if exists else HTTPStatus.ACCEPTED,
            headers={"Retry-After": "3"} if not exists else None,
        )

    def _admin_clear_jobs(self) -> None:
        if not self._authorized():
            return
        try:
            self._read_json()
            result = self.store.clear_jobs()
        except Exception as exc:
            self._json({"error": str(exc)}, status=HTTPStatus.SERVICE_UNAVAILABLE)
            return
        self._json(
            {
                "status": "ok",
                "cleared": result,
                "message": "Cleared terminal job history only.",
            }
        )

    def _admin_clear_diagnostics(self) -> None:
        if not self._authorized():
            return
        try:
            self._read_json()
            result = self.store.clear_diagnostics()
        except Exception as exc:
            self._json({"error": str(exc)}, status=HTTPStatus.SERVICE_UNAVAILABLE)
            return
        self._json(
            {
                "status": "ok",
                "cleared": result,
                "message": "Cleared worker heartbeats and terminal dirty-tile diagnostics only.",
            }
        )

    def _admin_create_job(self, path: str) -> None:
        if not self._authorized():
            return
        try:
            payload = self._read_json()
        except Exception as exc:
            self._json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        kind = JOB_ENDPOINTS[path]
        try:
            _prevalidate_admin_job(self.config, kind, payload)
            job_id = self.store.create_job(kind, payload)
        except ValueError as exc:
            self._json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        except Exception as exc:
            self._json({"error": str(exc)}, status=HTTPStatus.SERVICE_UNAVAILABLE)
            return
        self._json({"id": job_id, "kind": kind, "status": "pending"}, status=HTTPStatus.ACCEPTED)

    def _admin_list_jobs(self) -> None:
        if not self._authorized():
            return
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        try:
            limit = int(params.get("limit", ["50"])[0])
        except ValueError:
            limit = 50
        status = params.get("status", [None])[0] or None
        try:
            jobs = self.store.list_jobs(limit=limit, status=status)
        except Exception as exc:
            self._json({"error": str(exc)}, status=HTTPStatus.SERVICE_UNAVAILABLE)
            return
        self._json({"jobs": jobs, "limit": max(1, min(limit, 200)), "status": status})

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

    def _admin_validate_uploaded_bundle(self) -> None:
        if not self._authorized():
            return
        try:
            path = self._read_uploaded_file()
            validation = MapBundleValidator(self.config).validate(str(path))
            result = validation.to_dict()
            if validation.root:
                result["checksum"] = bundle_checksum(validation.root)
            self._json(result, status=HTTPStatus.OK if validation.valid else HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self._json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

    def _admin_panel(self) -> None:
        if not self.config.admin_enabled:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self._static_file("web-ui/index.html")

    def _home_panel(self) -> None:
        self._static_file("web-ui/index.html")

    def _map_viewer(self) -> None:
        self._static_file("web-ui/index.html")

    def _static_asset(self, path: str) -> None:
        name = path.removeprefix("/static/")
        self._static_file(name)

    def _static_file(self, name: str) -> None:
        base = STATIC_DIR.resolve()
        target = (base / name).resolve()
        try:
            target.relative_to(base)
        except ValueError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_type = STATIC_CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
        self._bytes(target.read_bytes(), content_type, headers={"Cache-Control": "no-store"})

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

    def _read_uploaded_file(self) -> Path:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            raise ValueError("Upload body is required")
        if length > MAX_BUNDLE_UPLOAD_BYTES:
            raise ValueError("Uploaded bundle is too large")
        content_type = self.headers.get("Content-Type", "")
        body = self.rfile.read(length)
        if content_type.startswith("multipart/form-data"):
            boundary = _multipart_boundary(content_type)
            filename, data = _multipart_file(body, boundary)
            suffixes = "".join(Path(filename).suffixes) or ".bundle"
        else:
            data = body
            suffixes = ".bundle"
        fd, target = tempfile.mkstemp(prefix=f"opentilesx-upload-{uuid.uuid4().hex}-", suffix=suffixes)
        with open(fd, "wb", closefd=True) as handle:
            handle.write(data)
        return Path(target)

    def _json(
        self,
        payload: dict[str, Any],
        status: HTTPStatus = HTTPStatus.OK,
        headers: dict[str, str] | None = None,
    ) -> None:
        data = json.dumps(payload, sort_keys=True).encode("utf-8")
        self._bytes(data, "application/json", status=status, headers=headers)

    def _text(self, payload: str, content_type: str = "text/plain") -> None:
        data = payload.encode("utf-8")
        self._bytes(data, content_type)

    def _bytes(
        self,
        payload: bytes,
        content_type: str,
        status: HTTPStatus = HTTPStatus.OK,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)


def _multipart_boundary(content_type: str) -> bytes:
    for part in content_type.split(";"):
        part = part.strip()
        if part.startswith("boundary="):
            value = part.split("=", 1)[1].strip().strip('"')
            return value.encode("utf-8")
    raise ValueError("multipart boundary is missing")


def _multipart_file(body: bytes, boundary: bytes) -> tuple[str, bytes]:
    marker = b"--" + boundary
    for part in body.split(marker):
        part = part.strip()
        if not part or part == b"--" or b"\r\n\r\n" not in part:
            continue
        header_bytes, data = part.split(b"\r\n\r\n", 1)
        headers = header_bytes.decode("utf-8", errors="replace")
        if "filename=" not in headers:
            continue
        filename = "uploaded.bundle"
        for header in headers.split("\r\n"):
            if header.lower().startswith("content-disposition:"):
                for item in header.split(";"):
                    item = item.strip()
                    if item.startswith("filename="):
                        filename = item.split("=", 1)[1].strip().strip('"') or filename
        if data.endswith(b"\r\n--"):
            data = data[:-4]
        elif data.endswith(b"\r\n"):
            data = data[:-2]
        return filename, data
    raise ValueError("multipart upload must include a file field")


def _render_worker_health(heartbeat: dict[str, Any] | None, poll_interval: float) -> dict[str, Any]:
    stale_after = max(60, int(poll_interval * 3))
    if not heartbeat:
        return {"health": "missing", "stale_after_seconds": stale_after}
    result = dict(heartbeat)
    result["stale_after_seconds"] = stale_after
    last_seen = _parse_timestamp(result.get("last_seen"))
    if not last_seen:
        result["health"] = "missing"
        return result
    age = max(0, int((datetime.now(timezone.utc) - last_seen).total_seconds()))
    result["age_seconds"] = age
    result["health"] = "stale" if age > stale_after else "healthy"
    return result


def _render_worker_summary(workers: list[dict[str, Any]], poll_interval: float) -> dict[str, Any]:
    stale_after = max(60, int(poll_interval * 3))
    counts = {"healthy": 0, "stale": 0, "missing": 0}
    processed = 0
    last_seen_values = []
    last_error = None
    for worker in workers:
        health = worker.get("health") or "missing"
        counts[health] = counts.get(health, 0) + 1
        processed += int(worker.get("processed_count") or 0)
        if worker.get("last_seen"):
            last_seen_values.append(worker["last_seen"])
        if worker.get("last_error"):
            last_error = worker["last_error"]
    if counts.get("healthy"):
        health = "healthy"
    elif counts.get("stale"):
        health = "stale"
    else:
        health = "missing"
    return {
        "health": health,
        "total": len(workers),
        "healthy": counts.get("healthy", 0),
        "stale": counts.get("stale", 0),
        "missing": counts.get("missing", 0),
        "processed_count": processed,
        "last_seen": max(last_seen_values) if last_seen_values else None,
        "last_error": last_error,
        "stale_after_seconds": stale_after,
    }


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _storage_summary(config: AppConfig) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "type": config.tile_store,
        "map_version": config.map_version,
        "default_layer": config.default_layer,
    }
    if config.tile_store == "filesystem":
        summary["filesystem"] = {"path": config.tile_fs_path}
    elif config.tile_store == "s3" and config.s3:
        summary["s3"] = {
            "bucket": config.s3.bucket,
            "prefix": config.s3.prefix,
            "region": config.s3.region,
            "endpoint_url": config.s3.endpoint_url,
            "force_path_style": config.s3.force_path_style,
        }
    return summary


def _rendering_summary(config: AppConfig) -> dict[str, Any]:
    return {
        "backend": config.render_backend,
        "backend_url": config.render_backend_url,
        "worker_processes": config.render_worker_processes,
        "metatile_size": config.metatile_size,
        "store_full_metatile": config.store_full_metatile,
        "import_threads": config.import_threads,
        "threads_compat": config.threads,
    }


def _database_summary(config: AppConfig) -> dict[str, str]:
    return {
        "render_database_url": mask_secret(config.render_database_url),
        "import_database_url": mask_secret(config.import_database_url),
        "control_database_url": mask_secret(config.control_database_url),
    }


def _tile_url(tile: TileRef, default_layer: str) -> str:
    if tile.layer == default_layer:
        return f"/tile/{tile.z}/{tile.x}/{tile.y}.png"
    return f"/tile/{tile.layer}/{tile.z}/{tile.x}/{tile.y}.png"


def _style_dir_for_summary(config: AppConfig) -> Path | None:
    try:
        if config.style_xml:
            return Path(config.style_xml).resolve().parent
        if config.map_bundle_uri:
            validation = MapBundleValidator(config).validate(config.map_bundle_uri)
            if validation.root:
                style = validation.manifest.get("style") or {}
                return (validation.root / style.get("directory", ".")).resolve()
        if Path("/data/style").is_dir():
            return Path("/data/style").resolve()
        default = Path("/opt/openstreetmap-carto-default")
        if default.is_dir():
            return default.resolve()
    except Exception:
        return None
    return None


def _admin_capabilities_payload(config: AppConfig) -> dict[str, Any]:
    internal_reason = (
        ""
        if config.allow_internal_external_data_downloads
        else "Set ALLOW_INTERNAL_EXTERNAL_DATA_DOWNLOADS=true to allow internal external-data downloads."
    )
    public_reason = (
        ""
        if config.allow_public_external_data_downloads
        else "Set ALLOW_PUBLIC_EXTERNAL_DATA_DOWNLOADS=true to allow public external-data downloads."
    )
    default_mode_message = ""
    if config.external_data_mode == "placeholder":
        default_mode_message = (
            "EXTERNAL_DATA_MODE=placeholder means real external-data loading is not selected by default."
        )
    elif config.external_data_mode == "auto":
        default_mode_message = (
            "EXTERNAL_DATA_MODE=auto prefers vendored or preloaded assets and falls back to placeholders when none are available."
        )
    elif config.external_data_mode == "local":
        default_mode_message = "EXTERNAL_DATA_MODE=local expects vendored or preloaded offline external-data assets."
    elif config.external_data_mode == "fetch":
        default_mode_message = "EXTERNAL_DATA_MODE=fetch expects downloads to be permitted by the matching source policy."
    return {
        "external_data": {
            "default_mode": config.external_data_mode,
            "allow_internal_downloads": config.allow_internal_external_data_downloads,
            "allow_public_downloads": config.allow_public_external_data_downloads,
            "default_mode_message": default_mode_message,
            "modes": {
                "local": {"allowed": True, "reason": ""},
                "placeholder": {"allowed": True, "reason": ""},
                "fetch_internal": {
                    "allowed": config.allow_internal_external_data_downloads,
                    "reason": internal_reason,
                },
                "fetch_public": {
                    "allowed": config.allow_public_external_data_downloads,
                    "reason": public_reason,
                },
            },
        },
        "actions": {
            "import": _external_data_action_flags(config),
            "reimport": _external_data_action_flags(config),
            "external-data/load": _external_data_action_flags(config),
            "bundle_generate": {
                "fetch_internal": {
                    "allowed": config.allow_internal_external_data_downloads,
                    "reason": internal_reason,
                },
                "fetch_public": {
                    "allowed": config.allow_public_external_data_downloads,
                    "reason": public_reason,
                },
                "fetch_local": {
                    "allowed": False,
                    "reason": "source_mode=local cannot fetch external data. Use a vendored preload or switch to internal/public.",
                },
            },
        },
    }


def _external_data_action_flags(config: AppConfig) -> dict[str, Any]:
    return {
        "local": {"allowed": True, "reason": ""},
        "placeholder": {"allowed": True, "reason": ""},
        "fetch_internal": {
            "allowed": config.allow_internal_external_data_downloads,
            "reason": ""
            if config.allow_internal_external_data_downloads
            else "Set ALLOW_INTERNAL_EXTERNAL_DATA_DOWNLOADS=true to allow internal external-data downloads.",
        },
        "fetch_public": {
            "allowed": config.allow_public_external_data_downloads,
            "reason": ""
            if config.allow_public_external_data_downloads
            else "Set ALLOW_PUBLIC_EXTERNAL_DATA_DOWNLOADS=true to allow public external-data downloads.",
        },
    }


def _prevalidate_admin_job(config: AppConfig, kind: str, payload: dict[str, Any]) -> None:
    if kind in {"import", "reimport", "external_data_load"}:
        mode = str(payload.get("external_data_mode") or config.external_data_mode or "").strip().lower()
        if mode == "fetch":
            source_mode = str(payload.get("external_data_source_mode") or "public").strip().lower()
            _validate_external_data_fetch_policy(config, source_mode)
        external_data_uri = str(payload.get("external_data_uri") or "").strip()
        if mode != "placeholder" and _uri_requires_download(external_data_uri):
            source_mode = str(payload.get("external_data_source_mode") or "local").strip().lower()
            _validate_external_data_fetch_policy(config, source_mode, context="External-data preload")
    if kind == "bundle_generate" and bool(payload.get("fetch_external_data")):
        source_mode = str(payload.get("source_mode") or config.import_source_mode or "local").strip().lower()
        _validate_external_data_fetch_policy(config, source_mode, bundle_generation=True)
    if kind == "bundle_generate":
        external_data_uri = str(payload.get("external_data_uri") or "").strip()
        if _uri_requires_download(external_data_uri):
            source_mode = str(payload.get("source_mode") or "local").strip().lower()
            _validate_external_data_fetch_policy(config, source_mode, context="Bundle external-data preload")


def _validate_external_data_fetch_policy(
    config: AppConfig,
    source_mode: str,
    bundle_generation: bool = False,
    context: str | None = None,
) -> None:
    context = context or ("Bundle generation" if bundle_generation else "External-data fetch")
    if source_mode == "internal":
        if not config.allow_internal_external_data_downloads:
            raise ValueError(
                f"{context} is blocked. Set ALLOW_INTERNAL_EXTERNAL_DATA_DOWNLOADS=true to allow internal external-data downloads."
            )
        return
    if source_mode == "public":
        if not config.allow_public_external_data_downloads:
            raise ValueError(
                f"{context} is blocked. Set ALLOW_PUBLIC_EXTERNAL_DATA_DOWNLOADS=true to allow public external-data downloads."
            )
        return
    if source_mode == "local":
        raise ValueError(
            f"{context} cannot use source_mode=local. Use vendored assets or switch to internal/public with the matching ALLOW_* policy."
        )
    raise ValueError("external_data_source_mode/source_mode must be one of local, internal, public")


def _uri_requires_download(uri: str) -> bool:
    if not uri:
        return False
    return urlparse(uri).scheme in {"http", "https", "s3", "oci"}


def run_server(config: AppConfig, storage: TileStorage, store: JobStore) -> None:
    handler = type(
        "ConfiguredTileApiHandler",
        (TileApiHandler,),
        {"config": config, "storage": storage, "store": store},
    )
    server = ThreadingHTTPServer((config.listen_host, config.listen_port), handler)
    LOGGER.info("tile-api listening host=%s port=%s tile_store=%s", config.listen_host, config.listen_port, config.tile_store)
    server.serve_forever()

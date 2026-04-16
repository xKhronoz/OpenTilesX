from __future__ import annotations

import os
import sys
import time
import urllib.request
from dataclasses import replace
from pathlib import Path

from .api import run_server
from .bundles import MapBundleValidator
from .config import AppConfig, ConfigError
from .jobs import CommandBuilder, JobRunner, JobStore
from .rendering import RenderWorker
from .storage import build_storage


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        _usage()
        return 2

    command = argv[0]
    try:
        if command == "tile-api":
            config = AppConfig.from_env("tile-api")
            store = JobStore(config.job_database_url or "")
            config = _apply_active_bundle(config, store)
            run_server(config, build_storage(config), store)
            return 0
        if command == "admin-worker":
            return _admin_worker()
        if command == "render-worker":
            return _render_worker()
        if command == "bootstrap":
            config = AppConfig.from_env("bootstrap")
            store = JobStore(config.database_admin_url or config.import_database_url or "")
            store.ensure_schema(create_extensions=bool(config.database_admin_url))
            print("tile_admin schema is ready")
            return 0
        if command == "import-once":
            return _import_once()
        if command == "update-once":
            return _update_once()
        if command == "validate-bundle":
            return _validate_bundle(argv[1:])
        if command == "healthcheck":
            return _healthcheck()
        _usage()
        return 2
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 64


def _admin_worker() -> int:
    config = AppConfig.from_env("admin-worker")
    store = JobStore(config.import_database_url or config.database_admin_url or "")
    store.ensure_schema()
    runner = JobRunner(config, store)
    while True:
        processed = runner.run_once()
        if not processed:
            time.sleep(config.worker_poll_interval)


def _render_worker() -> int:
    config = AppConfig.from_env("render-worker")
    store = JobStore(config.job_database_url or "")
    config = _apply_active_bundle(config, store)
    worker = RenderWorker(config, store, build_storage(config))
    worker.run_forever()
    return 0


def _import_once() -> int:
    config = AppConfig.from_env("import-once")
    payload = _payload_from_env(default_pbf="/data/region.osm.pbf")
    command = CommandBuilder(config).import_command(payload, append=False)
    print(" ".join(CommandBuilder.masked(command)))
    if not config.dry_run:
        import subprocess

        subprocess.run(command, check=True)
    return 0


def _update_once() -> int:
    config = AppConfig.from_env("update-once")
    payload = _payload_from_env(default_pbf="/data/changes.osc.gz")
    payload["change_uri"] = payload.pop("pbf_uri")
    command = CommandBuilder(config).update_command(payload)
    print(" ".join(CommandBuilder.masked(command)))
    if not config.dry_run:
        import subprocess

        subprocess.run(command, check=True)
    return 0


def _payload_from_env(default_pbf: str) -> dict[str, str]:
    pbf = os.getenv("PBF_URI") or os.getenv("PBF_PATH")
    if not pbf and Path(default_pbf).exists():
        pbf = default_pbf
    if not pbf:
        raise ConfigError("PBF_URI/PBF_PATH is required. Runtime imports never download sample data.")
    payload = {"pbf_uri": pbf}
    for env_name, payload_name in {
        "NAME_LUA": "lua",
        "NAME_STYLE": "style_file",
        "IMPORT_SCHEMA": "schema",
    }.items():
        if os.getenv(env_name):
            payload[payload_name] = os.environ[env_name]
    return payload


def _validate_bundle(args: list[str]) -> int:
    config = AppConfig.from_env("validate-bundle")
    uri = args[0] if args else config.map_bundle_uri
    if not uri:
        raise ConfigError("MAP_BUNDLE_URI or validate-bundle argument is required")
    result = MapBundleValidator(config).validate(uri)
    print(result.to_dict())
    return 0 if result.valid else 1


def _healthcheck() -> int:
    role = os.getenv("TILE_SERVER_ROLE", "tile-api")
    role = {"api": "tile-api", "renderer": "render-worker", "admin": "admin-worker"}.get(role, role)
    try:
        if role in {"tile-api", "admin-ui"}:
            return _http_healthcheck()
        if role == "render-worker":
            config = AppConfig.from_env("render-worker")
            return _db_healthcheck(config.job_database_url)
        if role == "admin-worker":
            config = AppConfig.from_env("admin-worker")
            database_url = config.import_database_url or config.database_admin_url or config.job_database_url
            return _db_healthcheck(database_url)

        config = AppConfig.from_env(role)
        if config.job_database_url:
            return _db_healthcheck(config.job_database_url)
        return 0
    except Exception as exc:
        print(f"healthcheck failed: {exc}", file=sys.stderr)
        return 1


def _http_healthcheck() -> int:
    port = os.getenv("LISTEN_PORT", "8080")
    url = f"http://127.0.0.1:{port}/healthz"
    with urllib.request.urlopen(url, timeout=2) as response:
        if 200 <= response.status < 300:
            return 0
        print(f"healthcheck failed: {url} returned HTTP {response.status}", file=sys.stderr)
        return 1


def _db_healthcheck(database_url: str | None) -> int:
    if not database_url:
        raise ConfigError("metadata database URL is required for worker healthcheck")
    with JobStore(database_url).connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            row = cur.fetchone()
    if not row or row[0] != 1:
        raise RuntimeError("database SELECT 1 did not return 1")
    return 0


def _apply_active_bundle(config: AppConfig, store: JobStore) -> AppConfig:
    try:
        active = store.get_setting("active_map_bundle")
    except Exception:
        return config
    if not active:
        return config
    updates = {}
    if not config.map_bundle_uri and active.get("uri"):
        updates["map_bundle_uri"] = active["uri"]
    if config.map_version == "default" and active.get("checksum"):
        updates["map_version"] = active["checksum"][:16]
    return replace(config, **updates) if updates else config


def _usage() -> None:
    print(
        "usage: run.sh <tile-api|render-worker|admin-worker|bootstrap|import-once|update-once|validate-bundle|healthcheck>",
        file=sys.stderr,
    )


if __name__ == "__main__":
    raise SystemExit(main())

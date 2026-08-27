from __future__ import annotations

import argparse
import json
import logging
import multiprocessing
import os
import signal
import subprocess
import sys
import time
import urllib.request
from dataclasses import replace
from pathlib import Path

from .api import run_server
from .bundle_generator import generate_map_bundle
from .bundles import MapBundleValidator
from .config import AppConfig, ConfigError, mask_secret
from .external_data import ExternalDataManager
from .imports import ImportInputResolver
from .jobs import CommandBuilder, JobRunner, JobStore
from .rendering import RenderWorker
from .storage import build_storage


LOGGER = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    _configure_logging()
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
            store.ensure_external_data_placeholders()
            print("tile_admin schema is ready")
            return 0
        if command == "import-once":
            return _import_once()
        if command == "update-once":
            return _update_once()
        if command == "validate-bundle":
            return _validate_bundle(argv[1:])
        if command == "generate-bundle":
            return _generate_bundle(argv[1:])
        if command == "prepare-external-data":
            return _prepare_external_data(argv[1:])
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
    LOGGER.info("admin-worker starting database=%s poll_interval=%s", _masked_database(config.import_database_url or config.database_admin_url), config.worker_poll_interval)
    while True:
        processed = runner.run_once()
        if not processed:
            time.sleep(config.worker_poll_interval)


def _render_worker() -> int:
    config = AppConfig.from_env("render-worker")
    store = JobStore(config.job_database_url or "")
    config = _apply_active_bundle(config, store)
    if config.render_worker_processes <= 1:
        worker = RenderWorker(config, store, build_storage(config))
        worker.run_forever()
        return 0

    processes: list[multiprocessing.Process] = []
    base_id = config.worker_id or "render-worker"

    def stop_children(*_args: object) -> None:
        for process in processes:
            if process.is_alive():
                process.terminate()

    signal.signal(signal.SIGTERM, stop_children)
    signal.signal(signal.SIGINT, stop_children)
    for index in range(config.render_worker_processes):
        child_config = replace(config, worker_id=f"{base_id}-{index + 1}")
        process = multiprocessing.Process(target=_render_worker_process, args=(child_config,))
        process.start()
        processes.append(process)
    for process in processes:
        process.join()
    return 0


def _render_worker_process(config: AppConfig) -> None:
    store = JobStore(config.job_database_url or "")
    worker = RenderWorker(config, store, build_storage(config))
    worker.run_forever()


def _configure_logging() -> None:
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
        force=True,
    )


def _masked_database(value: str | None) -> str:
    return mask_secret(value)


def _import_once() -> int:
    config = AppConfig.from_env("import-once")
    payload = _payload_from_env(default_pbf="/data/region.osm.pbf")
    builder = CommandBuilder(config)
    payload = builder._with_bundle_defaults(payload)
    payload, input_result = ImportInputResolver(config).resolve_payload(payload, job_id="import-once", dry_run=config.dry_run)
    command = builder.import_command(payload, append=False)
    print(" ".join(CommandBuilder.masked(command)))
    print(json.dumps({"input_resolution": input_result}, sort_keys=True))
    if not config.dry_run:
        subprocess.run(command, check=True)
        JobStore(config.import_database_url or config.database_admin_url or "").ensure_external_data_placeholders()
    return 0


def _update_once() -> int:
    config = AppConfig.from_env("update-once")
    payload = _payload_from_env(default_pbf="/data/changes.osc.gz")
    payload["change_uri"] = payload.pop("pbf_uri")
    command = CommandBuilder(config).update_command(payload)
    print(" ".join(CommandBuilder.masked(command)))
    if not config.dry_run:
        subprocess.run(command, check=True)
    return 0


def _payload_from_env(default_pbf: str) -> dict[str, str]:
    pbf = os.getenv("PBF_URI") or os.getenv("PBF_PATH") or os.getenv("DOWNLOAD_PBF")
    if not pbf and Path(default_pbf).exists():
        pbf = default_pbf
    if not pbf:
        raise ConfigError("PBF_URI/PBF_PATH or explicit DOWNLOAD_PBF is required. No sample data is downloaded implicitly.")
    payload = {"pbf_uri": pbf}
    poly = os.getenv("POLY_URI") or os.getenv("POLY_PATH") or os.getenv("DOWNLOAD_POLY")
    if not poly and Path("/data/region.poly").exists():
        poly = "/data/region.poly"
    if poly:
        payload["poly_uri"] = poly
    for env_name, payload_name in {
        "NAME_LUA": "lua",
        "NAME_STYLE": "style_file",
        "NAME_INDEXES": "indexes_sql",
        "IMPORT_SCHEMA": "schema",
        "IMPORT_SOURCE_MODE": "source_mode",
        "EXTERNAL_DATA_MODE": "external_data_mode",
        "EXTERNAL_DATA_SOURCE_MODE": "external_data_source_mode",
        "EXTERNAL_DATA_URI": "external_data_uri",
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


def _generate_bundle(args: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="run.sh generate-bundle")
    parser.add_argument("--style-dir", required=True)
    parser.add_argument("--output")
    parser.add_argument("--name", default="offline-map")
    parser.add_argument("--version", default="1")
    parser.add_argument("--pbf-uri", "--pbf", dest="pbf_uri")
    parser.add_argument("--poly-uri", "--poly", dest="poly_uri")
    parser.add_argument("--source-mode", choices=["local", "internal", "public"])
    parser.add_argument("--external-data-uri")
    parser.add_argument("--external-data-source-mode", choices=["local", "internal", "public"])
    parser.add_argument("--fetch-external-data", action="store_true")
    parsed = parser.parse_args(args)
    config = AppConfig.from_env("generate-bundle")
    payload = {
        "style_dir": parsed.style_dir,
        "output": parsed.output,
        "name": parsed.name,
        "version": parsed.version,
        "pbf_uri": parsed.pbf_uri,
        "poly_uri": parsed.poly_uri,
        "source_mode": parsed.source_mode,
        "external_data_uri": parsed.external_data_uri,
        "external_data_source_mode": parsed.external_data_source_mode,
        "fetch_external_data": parsed.fetch_external_data,
    }
    result = generate_map_bundle(config, {key: value for key, value in payload.items() if value is not None})
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _prepare_external_data(args: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="run.sh prepare-external-data")
    parser.add_argument("--style-dir", required=True)
    parser.add_argument("--source-mode", choices=["local", "internal", "public"], default="local")
    parser.add_argument("--external-data-uri")
    parser.add_argument("--fetch", action="store_true")
    parsed = parser.parse_args(args)
    config = AppConfig.from_env("prepare-external-data")
    result = ExternalDataManager(config).prepare_bundle(
        Path(parsed.style_dir),
        source_mode=parsed.source_mode,
        external_data_uri=parsed.external_data_uri,
        fetch=parsed.fetch,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("available") else 1


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
        "usage: run.sh <tile-api|render-worker|admin-worker|bootstrap|import-once|update-once|validate-bundle|generate-bundle|prepare-external-data|healthcheck>",
        "       run.sh prepare-external-data --style-dir /path/to/style [--external-data-uri /cache] [--source-mode local|internal|public] [--fetch]",
        file=sys.stderr,
    )


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


TRUE_VALUES = {"1", "true", "yes", "on", "enabled"}


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in TRUE_VALUES


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return int(value)


@dataclass(frozen=True)
class S3Config:
    bucket: str
    prefix: str
    region: Optional[str]
    endpoint_url: Optional[str]
    access_key_id: Optional[str]
    secret_access_key: Optional[str]
    force_path_style: bool


@dataclass(frozen=True)
class AppConfig:
    role: str
    render_database_url: Optional[str]
    import_database_url: Optional[str]
    control_database_url: Optional[str]
    database_admin_url: Optional[str]
    tile_store: str
    tile_fs_path: str
    s3: Optional[S3Config]
    map_version: str
    default_layer: str
    style_xml: Optional[str]
    style_workdir: str
    map_bundle_uri: Optional[str]
    admin_enabled: bool
    admin_token: Optional[str]
    listen_host: str
    listen_port: int
    worker_poll_interval: float
    worker_batch_size: int
    worker_id: Optional[str]
    metatile_size: int
    store_full_metatile: bool
    threads: int
    import_threads: int
    render_worker_processes: int
    render_backend: str
    render_backend_url: Optional[str]
    render_db_max_active: int
    render_db_statement_timeout_ms: int
    nearby_prefetch_enabled: bool
    nearby_prefetch_radius: int
    nearby_prefetch_priority: int
    osm2pgsql_extra_args: str
    import_source_mode: str
    allow_internal_import_downloads: bool
    allow_public_import_downloads: bool
    import_staging_path: str
    external_data_mode: str
    allow_internal_external_data_downloads: bool
    allow_public_external_data_downloads: bool
    external_data_staging_path: str
    bundle_output_dir: str
    allow_network_fetch: bool
    dry_run: bool

    @property
    def job_database_url(self) -> Optional[str]:
        return self.control_database_url or self.import_database_url or self.render_database_url

    @classmethod
    def from_env(cls, role: str) -> "AppConfig":
        tile_store = os.getenv("TILE_STORE", "filesystem").strip().lower()
        s3_config = None
        if tile_store == "s3":
            s3_config = S3Config(
                bucket=os.getenv("S3_BUCKET", ""),
                prefix=os.getenv("S3_PREFIX", ""),
                region=os.getenv("S3_REGION") or os.getenv("AWS_REGION"),
                endpoint_url=os.getenv("S3_ENDPOINT_URL"),
                access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
                secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
                force_path_style=env_bool("S3_FORCE_PATH_STYLE", True),
            )

        config = cls(
            role=role,
            render_database_url=os.getenv("RENDER_DATABASE_URL"),
            import_database_url=os.getenv("IMPORT_DATABASE_URL"),
            control_database_url=os.getenv("CONTROL_DATABASE_URL"),
            database_admin_url=os.getenv("DATABASE_ADMIN_URL"),
            tile_store=tile_store,
            tile_fs_path=os.getenv("TILE_FS_PATH", "/data/tiles"),
            s3=s3_config,
            map_version=os.getenv("MAP_VERSION", "default"),
            default_layer=os.getenv("DEFAULT_LAYER", "default"),
            style_xml=os.getenv("STYLE_XML"),
            style_workdir=os.getenv("STYLE_WORKDIR", "/tmp/tile-server/style"),
            map_bundle_uri=os.getenv("MAP_BUNDLE_URI"),
            admin_enabled=env_bool("ADMIN_ENABLED", False),
            admin_token=os.getenv("ADMIN_TOKEN"),
            listen_host=os.getenv("LISTEN_HOST", "0.0.0.0"),
            listen_port=env_int("LISTEN_PORT", 8080),
            worker_poll_interval=float(os.getenv("WORKER_POLL_INTERVAL", "5")),
            worker_batch_size=env_int("WORKER_BATCH_SIZE", 32),
            worker_id=os.getenv("WORKER_ID") or None,
            metatile_size=env_int("METATILE_SIZE", 8),
            store_full_metatile=env_bool("STORE_FULL_METATILE", True),
            threads=env_int("THREADS", 4),
            import_threads=env_int("IMPORT_THREADS", env_int("THREADS", 4)),
            render_worker_processes=env_int("RENDER_WORKER_PROCESSES", 1),
            render_backend=os.getenv("RENDER_BACKEND", "python-mapnik").strip().lower(),
            render_backend_url=os.getenv("RENDER_BACKEND_URL"),
            render_db_max_active=env_int("RENDER_DB_MAX_ACTIVE", 2),
            render_db_statement_timeout_ms=env_int("RENDER_DB_STATEMENT_TIMEOUT_MS", 30000),
            nearby_prefetch_enabled=env_bool("NEARBY_PREFETCH_ENABLED", True),
            nearby_prefetch_radius=env_int("NEARBY_PREFETCH_RADIUS", 1),
            nearby_prefetch_priority=env_int("NEARBY_PREFETCH_PRIORITY", 250),
            osm2pgsql_extra_args=os.getenv("OSM2PGSQL_EXTRA_ARGS", ""),
            import_source_mode=os.getenv("IMPORT_SOURCE_MODE", "local").strip().lower(),
            allow_internal_import_downloads=env_bool("ALLOW_INTERNAL_IMPORT_DOWNLOADS", False),
            allow_public_import_downloads=env_bool("ALLOW_PUBLIC_IMPORT_DOWNLOADS", False),
            import_staging_path=os.getenv("IMPORT_STAGING_PATH", "/tmp/tile-server/imports"),
            external_data_mode=os.getenv("EXTERNAL_DATA_MODE", "auto").strip().lower(),
            allow_internal_external_data_downloads=env_bool("ALLOW_INTERNAL_EXTERNAL_DATA_DOWNLOADS", False),
            allow_public_external_data_downloads=env_bool("ALLOW_PUBLIC_EXTERNAL_DATA_DOWNLOADS", False),
            external_data_staging_path=os.getenv("EXTERNAL_DATA_STAGING_PATH", "/tmp/tile-server/external-data"),
            bundle_output_dir=os.getenv("MAP_BUNDLE_OUTPUT_DIR", "/data/bundles/generated"),
            allow_network_fetch=env_bool("ALLOW_NETWORK_FETCH", False),
            dry_run=env_bool("DRY_RUN", False),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if self.tile_store not in {"filesystem", "s3"}:
            raise ConfigError("TILE_STORE must be either 'filesystem' or 's3'")
        if self.import_source_mode not in {"local", "internal", "public"}:
            raise ConfigError("IMPORT_SOURCE_MODE must be one of local, internal, public")
        if self.external_data_mode not in {"auto", "local", "fetch", "placeholder"}:
            raise ConfigError("EXTERNAL_DATA_MODE must be one of auto, local, fetch, placeholder")
        if self.render_backend not in {"python-mapnik", "http-sidecar"}:
            raise ConfigError("RENDER_BACKEND must be one of python-mapnik, http-sidecar")

        if self.tile_store == "s3":
            if not self.s3 or not self.s3.bucket:
                raise ConfigError("S3_BUCKET is required when TILE_STORE=s3")

        if self.admin_enabled and not self.admin_token:
            raise ConfigError("ADMIN_TOKEN is required when ADMIN_ENABLED=true")
        if self.role == "tile-api" and self.admin_enabled and not (self.control_database_url or self.import_database_url):
            raise ConfigError("CONTROL_DATABASE_URL or IMPORT_DATABASE_URL is required when ADMIN_ENABLED=true")

        if self.role in {"tile-api", "render-worker"} and not self.render_database_url:
            raise ConfigError(f"RENDER_DATABASE_URL is required for {self.role}")

        if self.role in {"admin-worker", "bootstrap", "import-once", "update-once"}:
            if not self.import_database_url and not self.database_admin_url:
                raise ConfigError(f"IMPORT_DATABASE_URL is required for {self.role}")

        if self.role in {"tile-api", "render-worker", "admin-worker"} and not self.job_database_url:
            raise ConfigError(f"A metadata database URL is required for {self.role}")
        if self.metatile_size < 1 or self.metatile_size > 16:
            raise ConfigError("METATILE_SIZE must be between 1 and 16")
        if self.import_threads < 1:
            raise ConfigError("IMPORT_THREADS must be at least 1")
        if self.render_worker_processes < 1:
            raise ConfigError("RENDER_WORKER_PROCESSES must be at least 1")
        if self.render_db_max_active < 1:
            raise ConfigError("RENDER_DB_MAX_ACTIVE must be at least 1")
        if self.render_db_statement_timeout_ms < 1:
            raise ConfigError("RENDER_DB_STATEMENT_TIMEOUT_MS must be at least 1")
        if self.nearby_prefetch_radius < 0:
            raise ConfigError("NEARBY_PREFETCH_RADIUS must be at least 0")
        if self.nearby_prefetch_priority < 1:
            raise ConfigError("NEARBY_PREFETCH_PRIORITY must be at least 1")
        if self.render_backend == "http-sidecar" and self.role == "render-worker" and not self.render_backend_url:
            raise ConfigError("RENDER_BACKEND_URL is required when RENDER_BACKEND=http-sidecar")


class ConfigError(RuntimeError):
    pass


def mask_secret(value: Optional[str]) -> str:
    if not value:
        return ""
    if "://" not in value:
        return "***"
    scheme, rest = value.split("://", 1)
    if "@" not in rest:
        return f"{scheme}://***"
    return f"{scheme}://***@{rest.split('@', 1)[1]}"

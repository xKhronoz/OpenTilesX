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
    threads: int
    osm2pgsql_extra_args: str
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
            threads=env_int("THREADS", 4),
            osm2pgsql_extra_args=os.getenv("OSM2PGSQL_EXTRA_ARGS", ""),
            allow_network_fetch=env_bool("ALLOW_NETWORK_FETCH", False),
            dry_run=env_bool("DRY_RUN", False),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if self.tile_store not in {"filesystem", "s3"}:
            raise ConfigError("TILE_STORE must be either 'filesystem' or 's3'")

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

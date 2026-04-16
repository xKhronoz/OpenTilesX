from __future__ import annotations

import json
import os
import shlex
import subprocess
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from .bundles import MapBundleValidator, bundle_checksum
from .config import AppConfig, mask_secret
from .tiles import TileRef


SCHEMA_SQL = """
CREATE SCHEMA IF NOT EXISTS tile_admin;

CREATE TABLE IF NOT EXISTS tile_admin.jobs (
    id uuid PRIMARY KEY,
    kind text NOT NULL,
    status text NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    result jsonb NOT NULL DEFAULT '{}'::jsonb,
    error text,
    attempts integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    finished_at timestamptz
);

CREATE INDEX IF NOT EXISTS jobs_status_created_idx
ON tile_admin.jobs (status, created_at);

CREATE TABLE IF NOT EXISTS tile_admin.dirty_tiles (
    id bigserial PRIMARY KEY,
    layer text NOT NULL,
    z integer NOT NULL,
    x integer NOT NULL,
    y integer NOT NULL,
    reason text NOT NULL DEFAULT 'missing',
    status text NOT NULL DEFAULT 'pending',
    attempts integer NOT NULL DEFAULT 0,
    available_after timestamptz NOT NULL DEFAULT now(),
    leased_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS dirty_tiles_status_idx
ON tile_admin.dirty_tiles (status, available_after, created_at);

CREATE UNIQUE INDEX IF NOT EXISTS dirty_tiles_active_unique_idx
ON tile_admin.dirty_tiles (layer, z, x, y)
WHERE status IN ('pending', 'leased');

CREATE TABLE IF NOT EXISTS tile_admin.settings (
    key text PRIMARY KEY,
    value jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
"""

EXTENSIONS_SQL = """
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS hstore;
"""


@dataclass(frozen=True)
class Job:
    id: str
    kind: str
    status: str
    payload: dict[str, Any]
    result: dict[str, Any]
    error: Optional[str]


class JobStore:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url

    @contextmanager
    def connect(self) -> Iterator[Any]:
        try:
            import psycopg2
            import psycopg2.extras
        except ImportError as exc:
            raise JobError("psycopg2 is required for database-backed jobs") from exc

        conn = psycopg2.connect(self.database_url)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def ensure_schema(self, create_extensions: bool = False) -> None:
        with self.connect() as conn:
            with conn.cursor() as cur:
                if create_extensions:
                    cur.execute(EXTENSIONS_SQL)
                cur.execute(SCHEMA_SQL)

    @contextmanager
    def advisory_lock(self, name: str) -> Iterator[bool]:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (name,))
                locked = bool(cur.fetchone()[0])
            try:
                yield locked
            finally:
                if locked:
                    with conn.cursor() as cur:
                        cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (name,))

    def create_job(self, kind: str, payload: dict[str, Any], status: str = "pending") -> str:
        job_id = str(uuid.uuid4())
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO tile_admin.jobs (id, kind, status, payload)
                    VALUES (%s, %s, %s, %s::jsonb)
                    """,
                    (job_id, kind, status, json.dumps(payload)),
                )
        return job_id

    def get_job(self, job_id: str) -> Optional[Job]:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, kind, status, payload, result, error FROM tile_admin.jobs WHERE id = %s",
                    (job_id,),
                )
                row = cur.fetchone()
        if not row:
            return None
        return Job(str(row[0]), row[1], row[2], dict(row[3]), dict(row[4]), row[5])

    def claim_next_job(self) -> Optional[Job]:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    WITH next_job AS (
                        SELECT id
                        FROM tile_admin.jobs
                        WHERE status = 'pending'
                        ORDER BY created_at
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                    )
                    UPDATE tile_admin.jobs j
                    SET status = 'running',
                        attempts = attempts + 1,
                        started_at = COALESCE(started_at, now()),
                        updated_at = now()
                    FROM next_job
                    WHERE j.id = next_job.id
                    RETURNING j.id, j.kind, j.status, j.payload, j.result, j.error
                    """
                )
                row = cur.fetchone()
        if not row:
            return None
        return Job(str(row[0]), row[1], row[2], dict(row[3]), dict(row[4]), row[5])

    def complete_job(self, job_id: str, result: dict[str, Any]) -> None:
        self._finish_job(job_id, "succeeded", result=result, error=None)

    def fail_job(self, job_id: str, error: str, result: Optional[dict[str, Any]] = None) -> None:
        self._finish_job(job_id, "failed", result=result or {}, error=error)

    def _finish_job(self, job_id: str, status: str, result: dict[str, Any], error: Optional[str]) -> None:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE tile_admin.jobs
                    SET status = %s, result = %s::jsonb, error = %s, finished_at = now(), updated_at = now()
                    WHERE id = %s
                    """,
                    (status, json.dumps(result), error, job_id),
                )

    def set_setting(self, key: str, value: dict[str, Any]) -> None:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO tile_admin.settings (key, value)
                    VALUES (%s, %s::jsonb)
                    ON CONFLICT (key)
                    DO UPDATE SET value = excluded.value, updated_at = now()
                    """,
                    (key, json.dumps(value)),
                )

    def get_setting(self, key: str) -> Optional[dict[str, Any]]:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM tile_admin.settings WHERE key = %s", (key,))
                row = cur.fetchone()
        return dict(row[0]) if row else None

    def enqueue_dirty_tile(self, tile: TileRef, reason: str = "missing") -> None:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO tile_admin.dirty_tiles (layer, z, x, y, reason)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT DO NOTHING
                    """,
                    (tile.layer, tile.z, tile.x, tile.y, reason),
                )

    def enqueue_dirty_tiles(self, tiles: Iterable[TileRef], reason: str = "manual") -> int:
        count = 0
        with self.connect() as conn:
            with conn.cursor() as cur:
                for tile in tiles:
                    cur.execute(
                        """
                        INSERT INTO tile_admin.dirty_tiles (layer, z, x, y, reason)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT DO NOTHING
                        """,
                        (tile.layer, tile.z, tile.x, tile.y, reason),
                    )
                    count += cur.rowcount
        return count

    def lease_dirty_tiles(self, limit: int) -> list[tuple[int, TileRef]]:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    WITH next_tiles AS (
                        SELECT id
                        FROM tile_admin.dirty_tiles
                        WHERE status = 'pending' AND available_after <= now()
                        ORDER BY created_at
                        FOR UPDATE SKIP LOCKED
                        LIMIT %s
                    )
                    UPDATE tile_admin.dirty_tiles d
                    SET status = 'leased',
                        attempts = attempts + 1,
                        leased_at = now(),
                        updated_at = now()
                    FROM next_tiles
                    WHERE d.id = next_tiles.id
                    RETURNING d.id, d.layer, d.z, d.x, d.y
                    """,
                    (limit,),
                )
                rows = cur.fetchall()
        return [(row[0], TileRef(row[1], row[2], row[3], row[4])) for row in rows]

    def complete_dirty_tile(self, dirty_id: int) -> None:
        self._finish_dirty_tile(dirty_id, "done", None)

    def fail_dirty_tile(self, dirty_id: int, error: str) -> None:
        self._finish_dirty_tile(dirty_id, "failed", error)

    def _finish_dirty_tile(self, dirty_id: int, status: str, error: Optional[str]) -> None:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE tile_admin.dirty_tiles
                    SET status = %s, reason = COALESCE(%s, reason), updated_at = now()
                    WHERE id = %s
                    """,
                    (status, error, dirty_id),
                )


class CommandBuilder:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def import_command(self, payload: dict[str, Any], append: bool = False) -> list[str]:
        payload = self._with_bundle_defaults(payload)
        pbf = payload.get("pbf_uri") or payload.get("pbf_path")
        if not pbf:
            raise JobError("Import jobs require pbf_uri or pbf_path")
        pbf_path = self._local_path(pbf)
        if append and pbf_path.endswith(".osm.pbf"):
            raise JobError("Full PBF files must use reimport/replace, not osm2pgsql append")

        command = [
            "osm2pgsql",
            "-d",
            self.config.import_database_url or self.config.database_admin_url or "",
            "--slim",
            "-G",
            "--hstore",
            "--number-processes",
            str(self.config.threads),
        ]
        command.append("--append" if append else "--create")

        schema = payload.get("schema")
        if schema:
            command.extend(["--schema", schema])

        style_args = self._style_args(payload)
        command.extend(style_args)

        if self.config.osm2pgsql_extra_args:
            command.extend(shlex.split(self.config.osm2pgsql_extra_args))

        command.append(pbf_path)
        return command

    def update_command(self, payload: dict[str, Any]) -> list[str]:
        payload = self._with_bundle_defaults(payload)
        change = payload.get("change_uri") or payload.get("change_path")
        if not change:
            raise JobError("Update jobs require change_uri/change_path or an internal replication feed wrapper")
        update_payload = dict(payload)
        update_payload["pbf_uri"] = change
        command = self.import_command(update_payload, append=True)
        expire_min = int(payload.get("expire_minzoom", os.getenv("EXPIRY_MINZOOM", "13")))
        expire_max = int(payload.get("expire_maxzoom", os.getenv("EXPIRY_MAXZOOM", "20")))
        command.insert(command.index("--append") + 1, f"-e{expire_min}-{expire_max}")
        output = payload.get("expiry_file")
        if output:
            command.extend(["-o", output])
        return command

    def _style_args(self, payload: dict[str, Any]) -> list[str]:
        args: list[str] = []
        lua = payload.get("lua") or os.getenv("NAME_LUA")
        style = payload.get("style_file") or os.getenv("NAME_STYLE")
        if lua:
            args.extend(["--tag-transform-script", lua])
        if style:
            args.extend(["-S", style])
        return args

    def indexes_command(self, payload: dict[str, Any]) -> Optional[list[str]]:
        payload = self._with_bundle_defaults(payload)
        indexes_sql = payload.get("indexes_sql")
        if not indexes_sql:
            return None
        return ["psql", self.config.import_database_url or self.config.database_admin_url or "", "-f", indexes_sql]

    def _with_bundle_defaults(self, payload: dict[str, Any]) -> dict[str, Any]:
        uri = payload.get("map_bundle_uri") or self.config.map_bundle_uri
        if not uri:
            return payload
        validation = MapBundleValidator(self.config).validate(uri)
        if not validation.valid or not validation.root:
            raise JobError("; ".join(validation.errors) or "Invalid map bundle")

        merged = dict(payload)
        manifest = validation.manifest
        imports = manifest.get("imports") or {}
        if not merged.get("pbf_uri") and imports.get("pbf"):
            merged["pbf_uri"] = str(validation.root / imports["pbf"])
        if not merged.get("poly_uri") and imports.get("poly"):
            merged["poly_uri"] = str(validation.root / imports["poly"])

        style = manifest.get("style") or {}
        style_dir = validation.root / style.get("directory", ".")
        if not merged.get("lua") and style.get("lua"):
            merged["lua"] = str(style_dir / style["lua"])
        if not merged.get("style_file") and style.get("styleFile"):
            merged["style_file"] = str(style_dir / style["styleFile"])
        if not merged.get("indexes_sql") and style.get("indexesSql"):
            merged["indexes_sql"] = str(style_dir / style["indexesSql"])
        return merged

    def _local_path(self, uri: str) -> str:
        if uri.startswith("file://"):
            return uri[7:]
        if "://" in uri:
            raise JobError(f"Import/update input must be local or pre-staged for this worker: {uri}")
        return uri

    @staticmethod
    def masked(command: list[str]) -> list[str]:
        masked = list(command)
        for index, value in enumerate(masked):
            if value in {"-d", "--database"} and index + 1 < len(masked):
                masked[index + 1] = mask_secret(masked[index + 1])
        return masked


class JobRunner:
    def __init__(self, config: AppConfig, store: JobStore) -> None:
        self.config = config
        self.store = store
        self.commands = CommandBuilder(config)

    def run_once(self) -> bool:
        with self.store.advisory_lock("tile_admin.admin_worker") as locked:
            if not locked:
                return False
            job = self.store.claim_next_job()
            if not job:
                return False
            try:
                result = self._run_job(job)
                self.store.complete_job(job.id, result)
            except Exception as exc:
                self.store.fail_job(job.id, str(exc))
            return True

    def _run_job(self, job: Job) -> dict[str, Any]:
        if job.kind == "import":
            return self._run_import(job.payload, append=False)
        if job.kind == "update":
            return self._run_update(job.payload)
        if job.kind == "reimport":
            strategy = job.payload.get("replace_strategy", "blue_green")
            payload = dict(job.payload)
            if strategy == "blue_green":
                payload.setdefault("schema", f"osm_{uuid.uuid4().hex[:12]}")
            elif strategy != "in_place":
                raise JobError("replace_strategy must be blue_green or in_place")
            result = self._run_import(payload, append=False)
            result["replace_strategy"] = strategy
            if strategy == "blue_green":
                self.store.set_setting("active_schema", {"schema": payload["schema"]})
            return result
        if job.kind == "expire":
            return self._run_expire(job.payload)
        if job.kind == "bundle_validate":
            return self._run_bundle_validate(job.payload)
        if job.kind == "bundle_activate":
            return self._run_bundle_activate(job.payload)
        raise JobError(f"Unknown job kind: {job.kind}")

    def _run_import(self, payload: dict[str, Any], append: bool) -> dict[str, Any]:
        command = self.commands.import_command(payload, append=append)
        indexes_command = self.commands.indexes_command(payload)
        result = {
            "command": CommandBuilder.masked(command),
            "indexes_command": CommandBuilder.masked(indexes_command) if indexes_command else None,
            "dry_run": self.config.dry_run,
        }
        if not self.config.dry_run:
            subprocess.run(command, check=True)
            if indexes_command:
                subprocess.run(indexes_command, check=True)
        return result

    def _run_update(self, payload: dict[str, Any]) -> dict[str, Any]:
        command = self.commands.update_command(payload)
        result = {"command": CommandBuilder.masked(command), "dry_run": self.config.dry_run}
        if not self.config.dry_run:
            subprocess.run(command, check=True)
        return result

    def _run_expire(self, payload: dict[str, Any]) -> dict[str, Any]:
        tiles = []
        for entry in payload.get("tiles", []):
            tiles.append(TileRef(entry.get("layer", self.config.default_layer), int(entry["z"]), int(entry["x"]), int(entry["y"])))
        count = self.store.enqueue_dirty_tiles(tiles, reason=payload.get("reason", "manual-expire"))
        return {"enqueued": count}

    def _run_bundle_validate(self, payload: dict[str, Any]) -> dict[str, Any]:
        uri = payload.get("map_bundle_uri") or self.config.map_bundle_uri
        if not uri:
            raise JobError("map_bundle_uri is required")
        validation = MapBundleValidator(self.config).validate(uri)
        result = validation.to_dict()
        if not validation.valid:
            raise JobError("; ".join(validation.errors))
        if validation.root:
            result["checksum"] = bundle_checksum(validation.root)
        return result

    def _run_bundle_activate(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = self._run_bundle_validate(payload)
        self.store.set_setting(
            "active_map_bundle",
            {
                "uri": result["uri"],
                "checksum": result.get("checksum"),
                "manifest": result.get("manifest"),
            },
        )
        return result


class JobError(RuntimeError):
    pass

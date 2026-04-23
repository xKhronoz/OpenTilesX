from __future__ import annotations

import json
import os
import shlex
import subprocess
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from .bundle_generator import generate_map_bundle
from .bundles import MapBundleValidator, bundle_checksum
from .config import AppConfig, mask_secret
from .external_data import ExternalDataManager
from .imports import ImportInputResolver
from .tiles import TileRef, metatile_origin


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

ALTER TABLE tile_admin.dirty_tiles
    ADD COLUMN IF NOT EXISTS origin_x integer,
    ADD COLUMN IF NOT EXISTS origin_y integer,
    ADD COLUMN IF NOT EXISTS metatile_size integer NOT NULL DEFAULT 8,
    ADD COLUMN IF NOT EXISTS priority integer NOT NULL DEFAULT 100,
    ADD COLUMN IF NOT EXISTS request_count integer NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS requested_at timestamptz NOT NULL DEFAULT now();

UPDATE tile_admin.dirty_tiles
SET origin_x = COALESCE(origin_x, (x / GREATEST(metatile_size, 1)) * GREATEST(metatile_size, 1)),
    origin_y = COALESCE(origin_y, (y / GREATEST(metatile_size, 1)) * GREATEST(metatile_size, 1)),
    requested_at = COALESCE(requested_at, created_at)
WHERE origin_x IS NULL OR origin_y IS NULL OR requested_at IS NULL;

DROP INDEX IF EXISTS tile_admin.dirty_tiles_active_unique_idx;
DROP INDEX IF EXISTS dirty_tiles_active_unique_idx;
CREATE INDEX IF NOT EXISTS dirty_tiles_status_idx
ON tile_admin.dirty_tiles (status, priority DESC, available_after, requested_at, created_at);

CREATE UNIQUE INDEX IF NOT EXISTS dirty_tiles_active_metatile_unique_idx
ON tile_admin.dirty_tiles (layer, z, origin_x, origin_y, metatile_size)
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

EXTERNAL_DATA_PLACEHOLDERS_SQL = """
CREATE TABLE IF NOT EXISTS public.simplified_water_polygons (
    way geometry(MultiPolygon, 3857)
);
CREATE TABLE IF NOT EXISTS public.water_polygons (
    way geometry(MultiPolygon, 3857)
);
CREATE TABLE IF NOT EXISTS public.icesheet_polygons (
    way geometry(MultiPolygon, 3857)
);
CREATE TABLE IF NOT EXISTS public.icesheet_outlines (
    way geometry(MultiLineString, 3857),
    ice_edge text
);
CREATE TABLE IF NOT EXISTS public.ne_110m_admin_0_boundary_lines_land (
    way geometry(MultiLineString, 3857)
);
"""


@dataclass(frozen=True)
class Job:
    id: str
    kind: str
    status: str
    payload: dict[str, Any]
    result: dict[str, Any]
    error: Optional[str]


@dataclass(frozen=True)
class DirtyTileJob:
    id: int
    tile: TileRef
    origin: TileRef
    metatile_size: int
    priority: int
    request_count: int
    requested_at: Optional[datetime]
    reason: str
    attempts: int


@dataclass(frozen=True)
class DirtyTileEnqueueResult:
    status: str
    dirty_id: Optional[int] = None

    def __post_init__(self) -> None:
        if self.status not in {"created", "coalesced", "ignored"}:
            raise ValueError(f"Unsupported enqueue status: {self.status}")

    @property
    def created(self) -> bool:
        return self.status == "created"

    @property
    def coalesced(self) -> bool:
        return self.status == "coalesced"

    @property
    def ignored(self) -> bool:
        return self.status == "ignored"

    def __bool__(self) -> bool:
        return not self.ignored


DIRTY_TILE_PRIORITY = {
    "missing": 300,
    "nearby-prefetch": 250,
    "diagnostic-test-render": 200,
    "manual": 200,
    "manual-expire": 100,
}


def _dirty_tile_priority(reason: str, override: Optional[int] = None) -> int:
    if override is not None:
        return int(override)
    return DIRTY_TILE_PRIORITY.get(reason, 150)


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

    def ensure_external_data_placeholders(self) -> None:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(EXTERNAL_DATA_PLACEHOLDERS_SQL)

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

    @contextmanager
    def advisory_lock_first(self, names: Iterable[str]) -> Iterator[Optional[str]]:
        wanted = [name for name in names if name]
        with self.connect() as conn:
            locked_name: Optional[str] = None
            try:
                with conn.cursor() as cur:
                    for name in wanted:
                        cur.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (name,))
                        if bool(cur.fetchone()[0]):
                            locked_name = name
                            break
                yield locked_name
            finally:
                if locked_name:
                    with conn.cursor() as cur:
                        cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (locked_name,))

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

    def list_jobs(self, limit: int = 50, status: Optional[str] = None) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        params: list[Any] = []
        where = ""
        if status:
            where = "WHERE status = %s"
            params.append(status)
        params.append(limit)
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT id, kind, status, payload, result, error, attempts, created_at, updated_at, started_at, finished_at
                    FROM tile_admin.jobs
                    {where}
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    params,
                )
                rows = cur.fetchall()
        return [
            {
                "id": str(row[0]),
                "kind": row[1],
                "status": row[2],
                "payload": dict(row[3]),
                "result": dict(row[4]),
                "error": row[5],
                "attempts": row[6],
                "created_at": row[7].isoformat() if row[7] else None,
                "updated_at": row[8].isoformat() if row[8] else None,
                "started_at": row[9].isoformat() if row[9] else None,
                "finished_at": row[10].isoformat() if row[10] else None,
            }
            for row in rows
        ]

    def clear_jobs(self, statuses: Iterable[str] = ("succeeded", "failed")) -> dict[str, Any]:
        wanted = [status for status in statuses if status]
        if not wanted:
            return {"deleted": 0, "by_status": {}}
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM tile_admin.jobs
                    WHERE status = ANY(%s)
                    RETURNING status
                    """,
                    (wanted,),
                )
                rows = cur.fetchall()
        counts: dict[str, int] = {}
        for row in rows:
            counts[row[0]] = counts.get(row[0], 0) + 1
        return {"deleted": len(rows), "by_status": counts}

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

    def set_render_worker_heartbeat(self, worker_id: str | dict[str, Any], value: Optional[dict[str, Any]] = None) -> None:
        if value is None:
            value = dict(worker_id) if isinstance(worker_id, dict) else {}
            worker_id = value.get("worker_id") or "default"
        heartbeat = dict(value)
        heartbeat["worker_id"] = str(worker_id)
        self.set_setting(f"render_worker_heartbeat:{worker_id}", heartbeat)
        self.set_setting("render_worker_heartbeat", heartbeat)

    def render_worker_heartbeats(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT key, value, updated_at
                    FROM tile_admin.settings
                    WHERE key = 'render_worker_heartbeat'
                       OR key LIKE 'render_worker_heartbeat:%'
                    ORDER BY updated_at DESC
                    """
                )
                rows = cur.fetchall()
        specific = [row for row in rows if row[0] != "render_worker_heartbeat"]
        selected = specific or rows
        heartbeats = []
        for key, value, updated_at in selected:
            heartbeat = dict(value)
            heartbeat.setdefault("worker_id", key.split(":", 1)[1] if ":" in key else "default")
            heartbeat["setting_key"] = key
            heartbeat["setting_updated_at"] = updated_at.isoformat() if updated_at else None
            heartbeats.append(heartbeat)
        return heartbeats

    def dirty_tile_counts(self) -> dict[str, Any]:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT status, count(*) FROM tile_admin.dirty_tiles GROUP BY status")
                counts = {row[0]: row[1] for row in cur.fetchall()}
                cur.execute(
                    """
                    SELECT
                        count(*) AS total_rows,
                        COALESCE(sum(request_count), 0) AS total_requests
                    FROM tile_admin.dirty_tiles
                    """
                )
                totals_row = cur.fetchone()
                cur.execute(
                    """
                    SELECT
                        min(created_at) FILTER (WHERE status = 'pending') AS oldest_pending,
                        min(leased_at) FILTER (WHERE status = 'leased') AS oldest_leased
                    FROM tile_admin.dirty_tiles
                    """
                )
                row = cur.fetchone()
                cur.execute(
                    """
                    SELECT priority, min(requested_at)
                    FROM tile_admin.dirty_tiles
                    WHERE status = 'pending'
                    GROUP BY priority
                    ORDER BY priority DESC
                    """
                )
                pending_by_priority = {
                    int(priority): timestamp.isoformat()
                    for priority, timestamp in cur.fetchall()
                    if timestamp is not None
                }
        return {
            "counts": counts,
            "oldest_pending_at": row[0].isoformat() if row and row[0] else None,
            "oldest_leased_at": row[1].isoformat() if row and row[1] else None,
            "request_count_total": int(totals_row[1] or 0) if totals_row else 0,
            "coalesced_requests": max(0, int((totals_row[1] or 0) - (totals_row[0] or 0))) if totals_row else 0,
            "oldest_pending_by_priority": pending_by_priority,
        }

    def recent_failed_dirty_tiles(self, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, layer, z, x, y, origin_x, origin_y, metatile_size, priority, request_count, attempts, reason, created_at, leased_at, updated_at
                    FROM tile_admin.dirty_tiles
                    WHERE status = 'failed'
                    ORDER BY updated_at DESC
                    LIMIT %s
                    """,
                    (limit,),
                )
                rows = cur.fetchall()
        return [
            {
                "id": row[0],
                "layer": row[1],
                "z": row[2],
                "x": row[3],
                "y": row[4],
                "origin_x": row[5],
                "origin_y": row[6],
                "metatile_size": row[7],
                "priority": row[8],
                "request_count": row[9],
                "attempts": row[10],
                "error": row[11],
                "created_at": row[12].isoformat() if row[12] else None,
                "leased_at": row[13].isoformat() if row[13] else None,
                "updated_at": row[14].isoformat() if row[14] else None,
            }
            for row in rows
        ]

    def dirty_tile_hotspots(self, limit: int = 10) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 50))
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT layer, z, origin_x, origin_y, metatile_size, priority, request_count, status, requested_at, updated_at
                    FROM tile_admin.dirty_tiles
                    ORDER BY request_count DESC, requested_at DESC
                    LIMIT %s
                    """,
                    (limit,),
                )
                rows = cur.fetchall()
        return [
            {
                "layer": row[0],
                "z": row[1],
                "origin_x": row[2],
                "origin_y": row[3],
                "metatile_size": row[4],
                "priority": row[5],
                "request_count": row[6],
                "status": row[7],
                "requested_at": row[8].isoformat() if row[8] else None,
                "updated_at": row[9].isoformat() if row[9] else None,
            }
            for row in rows
        ]

    def clear_diagnostics(
        self,
        dirty_statuses: Iterable[str] = ("done", "failed"),
    ) -> dict[str, Any]:
        statuses = [status for status in dirty_statuses if status]
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM tile_admin.settings
                    WHERE key = 'render_worker_heartbeat'
                       OR key LIKE 'render_worker_heartbeat:%'
                    RETURNING key
                    """
                )
                heartbeat_rows = cur.fetchall()
                dirty_counts: dict[str, int] = {}
                dirty_deleted = 0
                if statuses:
                    cur.execute(
                        """
                        DELETE FROM tile_admin.dirty_tiles
                        WHERE status = ANY(%s)
                        RETURNING status
                        """,
                        (statuses,),
                    )
                    dirty_rows = cur.fetchall()
                    dirty_deleted = len(dirty_rows)
                    for row in dirty_rows:
                        dirty_counts[row[0]] = dirty_counts.get(row[0], 0) + 1
        self.set_setting(
            "render_worker_errors_cleared_at",
            {"timestamp": datetime.now(timezone.utc).isoformat()},
        )
        return {
            "heartbeats_deleted": len(heartbeat_rows),
            "dirty_tiles_deleted": dirty_deleted,
            "dirty_tiles_by_status": dirty_counts,
        }

    def enqueue_dirty_tile(
        self,
        tile: TileRef,
        reason: str = "missing",
        metatile_size: int = 8,
        priority: Optional[int] = None,
    ) -> DirtyTileEnqueueResult:
        origin = metatile_origin(tile, metatile_size)
        effective_priority = _dirty_tile_priority(reason, priority)
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO tile_admin.dirty_tiles (
                        layer, z, x, y, origin_x, origin_y, metatile_size, reason, priority, request_count, requested_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 1, now())
                    ON CONFLICT (layer, z, origin_x, origin_y, metatile_size)
                    WHERE status IN ('pending', 'leased')
                    DO UPDATE
                    SET reason = excluded.reason,
                        priority = GREATEST(tile_admin.dirty_tiles.priority, excluded.priority),
                        request_count = tile_admin.dirty_tiles.request_count + 1,
                        requested_at = now(),
                        available_after = LEAST(tile_admin.dirty_tiles.available_after, now()),
                        updated_at = now()
                    RETURNING id, (xmax = 0) AS created
                    """,
                    (
                        tile.layer,
                        tile.z,
                        tile.x,
                        tile.y,
                        origin.x,
                        origin.y,
                        metatile_size,
                        reason,
                        effective_priority,
                    ),
                )
                row = cur.fetchone()
        if not row:
            return DirtyTileEnqueueResult("ignored")
        return DirtyTileEnqueueResult("created" if row[1] else "coalesced", dirty_id=int(row[0]))

    def enqueue_dirty_tiles(
        self,
        tiles: Iterable[TileRef],
        reason: str = "manual",
        metatile_size: int = 8,
        priority: Optional[int] = None,
    ) -> int:
        count = 0
        with self.connect() as conn:
            with conn.cursor() as cur:
                for tile in tiles:
                    origin = metatile_origin(tile, metatile_size)
                    cur.execute(
                        """
                        INSERT INTO tile_admin.dirty_tiles (
                            layer, z, x, y, origin_x, origin_y, metatile_size, reason, priority, request_count, requested_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 1, now())
                        ON CONFLICT (layer, z, origin_x, origin_y, metatile_size)
                        WHERE status IN ('pending', 'leased')
                        DO UPDATE
                        SET reason = excluded.reason,
                            priority = GREATEST(tile_admin.dirty_tiles.priority, excluded.priority),
                            request_count = tile_admin.dirty_tiles.request_count + 1,
                            requested_at = now(),
                            available_after = LEAST(tile_admin.dirty_tiles.available_after, now()),
                            updated_at = now()
                        """,
                        (
                            tile.layer,
                            tile.z,
                            tile.x,
                            tile.y,
                            origin.x,
                            origin.y,
                            metatile_size,
                            reason,
                            _dirty_tile_priority(reason, priority),
                        ),
                    )
                    count += cur.rowcount
        return count

    def lease_dirty_tiles(self, limit: int) -> list[DirtyTileJob]:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    WITH next_tiles AS (
                        SELECT id
                        FROM tile_admin.dirty_tiles
                        WHERE status = 'pending' AND available_after <= now()
                        ORDER BY priority DESC, requested_at, created_at
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
                    RETURNING d.id, d.layer, d.z, d.x, d.y, d.origin_x, d.origin_y, d.metatile_size,
                              d.priority, d.request_count, d.requested_at, d.reason, d.attempts
                    """,
                    (limit,),
                )
                rows = cur.fetchall()
        return [
            DirtyTileJob(
                id=row[0],
                tile=TileRef(row[1], row[2], row[3], row[4]),
                origin=TileRef(row[1], row[2], row[5], row[6]),
                metatile_size=row[7],
                priority=row[8],
                request_count=row[9],
                requested_at=row[10],
                reason=row[11],
                attempts=row[12],
            )
            for row in rows
        ]

    def complete_dirty_tile(self, dirty_id: int) -> None:
        self._finish_dirty_tile(dirty_id, "done", None)

    def fail_dirty_tile(self, dirty_id: int, error: str) -> None:
        self._finish_dirty_tile(dirty_id, "failed", error)

    def release_dirty_tiles(self, dirty_ids: Iterable[int], delay_seconds: float = 1.0, reason: str = "retry") -> int:
        ids = list(dirty_ids)
        if not ids:
            return 0
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE tile_admin.dirty_tiles
                    SET status = 'pending',
                        reason = %s,
                        available_after = now() + (%s * interval '1 second'),
                        leased_at = NULL,
                        updated_at = now()
                    WHERE id = ANY(%s)
                    """,
                    (reason, delay_seconds, ids),
                )
                return cur.rowcount

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
            str(self.config.import_threads),
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
        indexes_sql = payload.get("indexes_sql") or os.getenv("NAME_INDEXES")
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
        merged.setdefault("style_dir", str(style_dir))
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
        self.imports = ImportInputResolver(config)
        self.external_data = ExternalDataManager(config)

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
            except CommandExecutionError as exc:
                self.store.fail_job(job.id, str(exc), exc.result)
            except Exception as exc:
                self.store.fail_job(job.id, str(exc))
            return True

    def _run_job(self, job: Job) -> dict[str, Any]:
        if job.kind == "import":
            return self._run_import(job.payload, append=False, job_id=job.id)
        if job.kind == "update":
            return self._run_update(job.payload)
        if job.kind == "reimport":
            strategy = job.payload.get("replace_strategy", "blue_green")
            payload = dict(job.payload)
            if strategy == "blue_green":
                payload.setdefault("schema", f"osm_{uuid.uuid4().hex[:12]}")
            elif strategy != "in_place":
                raise JobError("replace_strategy must be blue_green or in_place")
            result = self._run_import(payload, append=False, job_id=job.id)
            result["replace_strategy"] = strategy
            if strategy == "blue_green":
                self.store.set_setting("active_schema", {"schema": payload["schema"]})
            return result
        if job.kind == "expire":
            return self._run_expire(job.payload)
        if job.kind == "external_data_load":
            return self._run_external_data_load(job.payload, job_id=job.id)
        if job.kind == "bundle_validate":
            return self._run_bundle_validate(job.payload)
        if job.kind == "bundle_activate":
            return self._run_bundle_activate(job.payload)
        if job.kind == "bundle_generate":
            return self._run_bundle_generate(job.payload)
        raise JobError(f"Unknown job kind: {job.kind}")

    def _run_import(self, payload: dict[str, Any], append: bool, job_id: str = "manual") -> dict[str, Any]:
        payload = self.commands._with_bundle_defaults(payload)
        resolved_payload, input_result = self.imports.resolve_payload(payload, job_id=job_id, dry_run=self.config.dry_run)
        command = self.commands.import_command(resolved_payload, append=append)
        indexes_command = self.commands.indexes_command(resolved_payload)
        result = {
            "command": CommandBuilder.masked(command),
            "indexes_command": CommandBuilder.masked(indexes_command) if indexes_command else None,
            "input_resolution": input_result,
            "dry_run": self.config.dry_run,
        }
        style_dir = self._style_dir(resolved_payload)
        if not self.config.dry_run:
            self._validate_import_inputs(command, indexes_command)
            result["osm2pgsql"] = self._run_command(command)
            if indexes_command:
                result["indexes"] = self._run_command(indexes_command)
            external_plan = self.external_data.load(
                resolved_payload,
                style_dir,
                job_id=job_id,
                dry_run=False,
            )
            result["external_data"] = external_plan
            if external_plan.get("status") == "placeholder" and self.store:
                self.store.ensure_external_data_placeholders()
                result["external_data_placeholders"] = True
        else:
            result["external_data"] = self.external_data.load(
                resolved_payload,
                style_dir,
                job_id=job_id,
                dry_run=True,
            )
        return result

    def _run_update(self, payload: dict[str, Any]) -> dict[str, Any]:
        command = self.commands.update_command(payload)
        result = {"command": CommandBuilder.masked(command), "dry_run": self.config.dry_run}
        if not self.config.dry_run:
            self._validate_import_inputs(command, None)
            result["osm2pgsql"] = self._run_command(command)
        return result

    def _run_expire(self, payload: dict[str, Any]) -> dict[str, Any]:
        tiles = []
        for entry in payload.get("tiles", []):
            tiles.append(TileRef(entry.get("layer", self.config.default_layer), int(entry["z"]), int(entry["x"]), int(entry["y"])))
        count = self.store.enqueue_dirty_tiles(
            tiles,
            reason=payload.get("reason", "manual-expire"),
            metatile_size=self.config.metatile_size,
        )
        return {"enqueued": count}

    def _run_external_data_load(self, payload: dict[str, Any], job_id: str = "manual") -> dict[str, Any]:
        style_dir = self._style_dir(payload)
        result = {
            "style_dir": str(style_dir),
            "dry_run": self.config.dry_run,
        }
        plan = self.external_data.load(payload, style_dir, job_id=job_id, dry_run=self.config.dry_run)
        result["external_data"] = plan
        if plan.get("status") == "placeholder" and self.store:
            self.store.ensure_external_data_placeholders()
            result["external_data_placeholders"] = True
        return result

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

    def _run_bundle_generate(self, payload: dict[str, Any]) -> dict[str, Any]:
        return generate_map_bundle(self.config, payload)

    def _run_command(self, command: list[str]) -> dict[str, Any]:
        completed = subprocess.run(command, check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        result = {
            "command": CommandBuilder.masked(command),
            "returncode": completed.returncode,
            "stdout_tail": _tail(completed.stdout),
            "stderr_tail": _tail(completed.stderr),
        }
        if completed.returncode != 0:
            message = f"Command exited with status {completed.returncode}: {' '.join(CommandBuilder.masked(command))}"
            if result["stderr_tail"]:
                message = f"{message}\nstderr:\n{result['stderr_tail']}"
            elif result["stdout_tail"]:
                message = f"{message}\nstdout:\n{result['stdout_tail']}"
            raise CommandExecutionError(message, result)
        return result

    def _validate_import_inputs(self, command: list[str], indexes_command: Optional[list[str]]) -> None:
        if command:
            self._validate_file(command[-1], "import input")
        for flag, label in (("--tag-transform-script", "osm2pgsql Lua transform"), ("-S", "osm2pgsql style file")):
            if flag in command:
                index = command.index(flag)
                if index + 1 < len(command):
                    self._validate_file(command[index + 1], label, reject_placeholder=True)
        if indexes_command and "-f" in indexes_command:
            index = indexes_command.index("-f")
            if index + 1 < len(indexes_command):
                self._validate_file(indexes_command[index + 1], "post-import indexes SQL", reject_placeholder=True)

    def _validate_file(self, path: str, label: str, reject_placeholder: bool = False) -> None:
        file_path = Path(path)
        if not file_path.is_file():
            raise JobError(f"{label} does not exist or is not readable: {path}")
        if reject_placeholder and _is_placeholder_asset(file_path):
            raise JobError(
                f"{label} is a placeholder file: {path}. "
                "Replace examples/map-bundle with a real offline map bundle before running import jobs."
            )

    def _style_dir(self, payload: dict[str, Any]) -> Path:
        if payload.get("map_bundle_uri"):
            validation = MapBundleValidator(self.config).validate(str(payload["map_bundle_uri"]))
            if not validation.valid or not validation.root:
                raise JobError("; ".join(validation.errors) or "Invalid map bundle")
            style = validation.manifest.get("style") or {}
            style_dir = style.get("directory", ".")
            return (validation.root / style_dir).resolve()
        if payload.get("style_dir"):
            return Path(str(payload["style_dir"])).resolve()
        for key in ("lua", "style_file", "indexes_sql"):
            value = payload.get(key)
            if value:
                return Path(str(value)).resolve().parent
        if self.config.style_xml:
            return Path(self.config.style_xml).resolve().parent
        if Path("/data/style").is_dir():
            return Path("/data/style").resolve()
        return Path("/opt/openstreetmap-carto-default").resolve()


class JobError(RuntimeError):
    pass


class CommandExecutionError(JobError):
    def __init__(self, message: str, result: dict[str, Any]) -> None:
        super().__init__(message)
        self.result = result


def _tail(value: str, limit: int = 4000) -> str:
    if len(value) <= limit:
        return value
    return value[-limit:]


def _is_placeholder_asset(path: Path) -> bool:
    try:
        sample = path.read_text(errors="ignore")[:4096]
    except OSError:
        return False
    return "Placeholder for an offline map bundle" in sample or "Replace with the bundle" in sample

# Cloud-Native Tile Server Architecture

This project is now organized as a set of role-based containers that share one external PostgreSQL/PostGIS authority and one configured rendered-tile store. The runtime containers do not start PostgreSQL, Apache, nginx, cron, or renderd internally.

The design supports cloud deployments such as OCI, ordinary container hosts, and Kubernetes clusters. It also supports isolated environments where runtime containers cannot reach the public internet.

## Goals

- Keep PostgreSQL/PostGIS external and authoritative.
- Separate read-heavy tile serving from render work and import/update administration.
- Allow rendered tiles to live on either a shared filesystem or S3-compatible object storage.
- Make imports and updates explicit jobs controlled by one writer plane.
- Avoid hidden runtime downloads so air-gapped operation is predictable.
- Preserve compatibility shims for the historical `run` and `import` commands.

## Runtime Roles

### Tile API

Image target: `api`

Command: `tile-api`

The tile API serves:

- `GET /tile/{z}/{x}/{y}.png`
- `GET /tile/{layer}/{z}/{x}/{y}.png`
- `GET /healthz`
- Optional admin endpoints when `ADMIN_ENABLED=true`

The API reads rendered PNGs from the configured tile store. When a tile is missing, it enqueues a dirty-tile record in `tile_admin.dirty_tiles` and returns `404`. Render workers later consume that queue.

The API uses `RENDER_DATABASE_URL` for map reads and `CONTROL_DATABASE_URL`, `IMPORT_DATABASE_URL`, or `RENDER_DATABASE_URL` for metadata, in that order.

### Render Worker

Image target: `renderer`

Command: `render-worker`

Render workers claim pending dirty tiles, render Mapnik PNGs from external PostGIS, and write results to the configured tile store. Multiple workers can run at the same time because leases and job state are coordinated through PostgreSQL.

Render workers need `RENDER_DATABASE_URL` and a metadata database URL. In most deployments, metadata is provided by `CONTROL_DATABASE_URL`.

### Admin Worker

Image target: `admin-worker`

Command: `admin-worker`

The admin worker is the only role that performs import, update, reimport, map-bundle activation, and tile-expiry jobs. Jobs are stored in `tile_admin.jobs` and guarded with PostgreSQL advisory locks so that only one destructive writer action runs at a time.

The admin worker needs `IMPORT_DATABASE_URL` or `DATABASE_ADMIN_URL`.

### Admin UI

Image target: `admin-ui`

The admin UI uses the API runtime with `ADMIN_ENABLED=true`. The browser control panel is served by the API and does not load public CDN assets. Operators can create import/update/reimport/expire jobs and inspect job status through the admin API.

## Data And Control Planes

PostgreSQL/PostGIS is the single authoritative data plane. The app containers do not run an embedded database.

Database URLs:

- `RENDER_DATABASE_URL`: read path for API and render workers.
- `IMPORT_DATABASE_URL`: writer path for admin/import/update jobs.
- `CONTROL_DATABASE_URL`: metadata path for jobs, settings, dirty tiles, and active bundle state.
- `DATABASE_ADMIN_URL`: bootstrap-only path for extensions and schema creation.

Metadata lives in the `tile_admin` schema:

- `tile_admin.jobs`: admin/import/update/reimport/expire job records.
- `tile_admin.dirty_tiles`: render queue and retry state.
- `tile_admin.settings`: active bundle, active map version, active schema, and related settings.

Bootstrap creates the schema and, when allowed through `DATABASE_ADMIN_URL`, creates the `postgis` and `hstore` extensions.

## Tile Storage

Rendered tiles are addressed by layer, map version, and XYZ tile coordinates. Including the map version in the key prevents stale tiles from colliding with a newly activated map.

### Filesystem

Set:

```sh
TILE_STORE=filesystem
TILE_FS_PATH=/data/tiles
```

Filesystem mode writes PNGs atomically with a temporary file followed by rename. In Kubernetes, more than one API or renderer pod requires a `ReadWriteMany` volume. Bind mounts and externally provisioned PVCs must be writable by UID/GID `1000`.

### S3-Compatible Storage

Set:

```sh
TILE_STORE=s3
S3_BUCKET=tiles
S3_PREFIX=osm
S3_REGION=us-ashburn-1
S3_ENDPOINT_URL=https://namespace.compat.objectstorage.us-ashburn-1.oraclecloud.com
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
S3_FORCE_PATH_STYLE=true
```

S3 mode uses object API operations directly through the Python SDK. It does not use `s3fs` or object-storage filesystem mounts. OCI Object Storage is supported through its S3-compatible endpoint.

## Map Bundles

Runtime containers do not download public styles, overlays, fonts, sprites, external data, sample PBFs, or replication feeds. Operators provide those inputs through offline map bundles and local data paths.

`MAP_BUNDLE_URI` accepts:

- `file:///...`
- `s3://...`
- `oci://...`
- `http://internal/...` when `ALLOW_NETWORK_FETCH=true`

A bundle contains a manifest plus the assets needed to render and import a map:

- Mapnik XML style.
- osm2pgsql Lua/style files and optional SQL.
- Fonts, symbols, sprites, and external data.
- Optional local PBF, poly, or change-file references.
- Overlay definitions.
- Checksums for integrity checks.

The admin API can validate and activate bundles. Activation records the bundle URI and checksum in `tile_admin.settings`. API and renderer pods can then adopt the active bundle when `MAP_BUNDLE_URI` is not set explicitly.

## Imports, Updates, And Reimports

All writer jobs run through the admin worker.

Import and reimport behavior:

- Full PBF inputs use replace/reimport behavior.
- `osm2pgsql --append` is only used for change files against an existing slim database.
- Unsafe full-PBF append is rejected.

Supported strategies:

- `blue_green`: import into a new schema or target, validate it, switch active metadata, and expire affected tiles.
- `in_place`: recreate active OSM tables directly. This uses less storage but can degrade or interrupt rendering.
- `auto`: choose append only for replication/change files; choose replace for full PBF inputs.

Offline update modes:

- Internal replication mirror or local change-file feed.
- Manual full PBF reimport where no replication feed exists.

## Overlay Modes

Bundles can define overlays in two ways:

- Baked overlays: overlay data is loaded into PostGIS and rendered into the same PNG tiles as the base map.
- Separate overlay layers: a bundle defines additional layer names served through `/tile/{layer}/{z}/{x}/{y}.png`.

Separate layers can provide their own Mapnik XML or inherit the bundle default style.

## Airgap Operation

Runtime airgap support is strict by default:

- No Luxembourg fallback download.
- No implicit public PBF, poly, style, font, sprite, or external-data fetch.
- No implicit public replication feed.
- HTTP bundle access is disabled unless `ALLOW_NETWORK_FETCH=true`.

An airgap release normally contains:

- Role image tarballs.
- Docker Compose files or Kubernetes/Helm manifests.
- Map bundles and checksums.
- Local PBF, poly, and change files.
- Internal object-storage or filesystem configuration.

Fully offline image builds are not required by this design. Runtime operation after image delivery is offline-capable.

## Deployment Topologies

### Docker Compose With Filesystem Tiles

Use `docker-compose.yml` for a local external-PostGIS topology and a named filesystem tile cache. This mode is useful for development and small self-hosted installs.

### Docker Compose With S3-Compatible Tiles

Use `deploy/docker-compose.s3.yml` for MinIO-backed tile storage. The same settings map to OCI Object Storage or another S3-compatible service.

### Kubernetes Or Helm

Kubernetes deployments run separate API, renderer, and admin-worker pods. Filesystem mode requires a RWX tile PVC. S3 mode removes the shared tile-cache PVC from the hot path.

Pods run as UID/GID `1000`:

```yaml
securityContext:
  runAsNonRoot: true
  runAsUser: 1000
  runAsGroup: 1000
  fsGroup: 1000
```

Externally provisioned PVCs and bind mounts must be writable by UID/GID `1000`.

## Health Checks

The image exposes a role-aware `healthcheck` command:

- `tile-api` and `admin-ui`: `GET http://127.0.0.1:${LISTEN_PORT}/healthz`
- `render-worker`: validate config and run `SELECT 1` against the metadata database.
- `admin-worker`: validate config and run `SELECT 1` against the import/admin metadata database.

Docker images include a `HEALTHCHECK` instruction that calls `/run.sh healthcheck`.

## Image And Dependency Management

The Dockerfile builds one combined Debian slim runtime that is reused by all targets. Role targets only set the default role command; they do not remove packages or create role-specific dependency sets. This keeps API, renderer, admin-worker, admin-ui, and final images operationally interchangeable.

Native geospatial and database packages come from Debian packages:

- Mapnik and `mapnik-utils`.
- `osm2pgsql`, `osmium-tool`, and `osmosis`.
- PostgreSQL client tools.
- Python bindings supplied by Debian where they depend on native libraries, including Mapnik, psycopg2, lxml, and Shapely.

Python-only application dependencies are declared in `pyproject.toml` and pinned in `poetry.lock`. Poetry is a build-time tool only: the image build exports the locked main dependency set to a temporary requirements file, installs it into `/opt/tile-server-venv`, and discards Poetry before the runtime stage. The venv is created with `python3 -m venv --system-site-packages`, so Python packages installed from Poetry can coexist with Debian's native Python bindings. Poetry itself is not installed in the final runtime image.

This keeps the Dockerfile focused on operating system capabilities while `pyproject.toml` is the source of truth for Python SDK/client libraries such as S3 access and HTTP/YAML helpers.

## Compatibility

Legacy commands still route to the new role commands:

- `run` starts `tile-api`.
- `import` runs `import-once` against external PostgreSQL.

They no longer start bundled PostgreSQL, Apache, nginx, cron, or renderd.

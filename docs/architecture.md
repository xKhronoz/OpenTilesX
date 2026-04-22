# OpenTilesX Architecture

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

The API reads rendered PNGs from the configured tile store. When a tile is missing, it enqueues a dirty-tile record in `tile_admin.dirty_tiles` and returns `202` with `Retry-After`. Render workers later consume that queue.

The API uses `RENDER_DATABASE_URL` for map reads and `CONTROL_DATABASE_URL`, `IMPORT_DATABASE_URL`, or `RENDER_DATABASE_URL` for metadata, in that order.

### Render Worker

Image target: `renderer`

Command: `render-worker`

Render workers claim pending dirty tiles, render Mapnik PNGs from external PostGIS, and write results to the configured tile store. Multiple workers can run at the same time because leases and job state are coordinated through PostgreSQL.

Render workers need `RENDER_DATABASE_URL` and a metadata database URL. In most deployments, metadata is provided by `CONTROL_DATABASE_URL`.

Render workers use metatiles by default:

- `WORKER_ID` identifies each worker in diagnostics; when unset, it defaults to hostname and process id.
- `RENDER_WORKER_PROCESSES=1` controls how many child worker processes one `render-worker` container supervises.
- `RENDER_BACKEND=python-mapnik|http-sidecar` selects whether the worker renders locally with Python Mapnik or delegates metatile rendering to an HTTP sidecar.
- `RENDER_BACKEND_URL` is required when `RENDER_BACKEND=http-sidecar`.
- `METATILE_SIZE=8` renders an 8x8 group as one Mapnik image.
- `STORE_FULL_METATILE=true` stores every tile from that rendered group, not only the tile that triggered the miss.
- `WORKER_BATCH_SIZE=32` controls how many dirty records a worker leases before grouping them by metatile origin.

This makes the first request in a new area more expensive, but it warms surrounding tiles and avoids repeated style loads and repeated PostGIS reads for adjacent map-preview tiles. Smaller metatiles reduce memory and first-tile latency; larger values should be tested against worker memory, database load, and object-storage write throughput.

The default backend is `python-mapnik`. The optional `http-sidecar` backend keeps the same queueing and storage logic, but replaces the render step with `POST /render/metatile` and a tar archive response containing the rendered PNG tiles plus manifest metadata. This keeps the current architecture extensible without making `renderd` or another backend mandatory.

When multiple render workers run, each metatile render is guarded by a PostgreSQL advisory lock keyed by map version, layer, zoom, and metatile origin. A worker that leases dirty tiles for a busy metatile releases them back to pending with a short delay, preventing duplicate renders while keeping the queue live.

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

Bootstrap/import also ensures empty compatibility tables for OpenStreetMap Carto external-data layers such as `icesheet_polygons`, `icesheet_outlines`, water polygons, and Natural Earth boundary lines. Real offline bundles can replace or populate these tables with bundled external data; the empty tables prevent Mapnik from failing in air-gapped quick starts where the external datasets were not fetched.

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

The repository's `examples/map-bundle` is a structural example, not a production-ready Carto bundle. Import jobs reject its placeholder Lua/style/index assets before invoking `osm2pgsql`. Operators must replace it with a real offline map bundle, mount an internally assembled bundle in the same path, or intentionally use the image-bundled Carto defaults for local quick starts.

Bundles can be generated with `run.sh generate-bundle` or the admin `map-bundle/generate` job. The generator copies a prepared style directory, optional PBF/poly files, a manifest, and a checksum file into a `.tar.gz` archive. Carto external data is fetched only when requested and when the import source policy allows network access.

Bundle generation supports three external-data input patterns:

- Already vendored assets inside the source style directory.
- An explicit local preload directory or archive supplied through `external_data_uri`.
- A connected fetch path using the upstream Carto `get-external-data.py` workflow.

When the generator can materialize real external data, it also writes `style/external-data.offline.yml` with `file://` URLs pointing at vendored bundle assets and records that in the manifest `externalData` section. Bundle validation warns when `external-data.yml` exists but neither vendored assets nor an offline config are present.

## Imports, Updates, And Reimports

All writer jobs run through the admin worker.

Import and reimport behavior:

- Full PBF inputs use replace/reimport behavior.
- `osm2pgsql --append` is only used for change files against an existing slim database.
- Unsafe full-PBF append is rejected.
- Mounted `/data/region.osm.pbf`, optional `/data/region.poly`, `PBF_URI`/`PBF_PATH`, `POLY_URI`/`POLY_PATH`, `DOWNLOAD_PBF`, and `DOWNLOAD_POLY` are supported as explicit inputs.
- `IMPORT_SOURCE_MODE=local` accepts only mounted paths and `file://` URIs. `internal` allows internal HTTP/S, S3, or OCI inputs when `ALLOW_INTERNAL_IMPORT_DOWNLOADS=true`. `public` allows public HTTP/S only when `ALLOW_PUBLIC_IMPORT_DOWNLOADS=true`.
- Downloaded import inputs are staged under `/tmp/tile-server/imports/<job-id>/` and recorded with path, source URI, size, and SHA-256 metadata.

External data loading is explicit too:

- `EXTERNAL_DATA_MODE=auto`: prefer vendored bundle data or a staged local preload, otherwise fall back to placeholder tables.
- `EXTERNAL_DATA_MODE=local`: require vendored or preloaded offline assets and run `get-external-data.py` against an offline config.
- `EXTERNAL_DATA_MODE=fetch`: run the upstream-style fetch only when the matching internal/public external-data download policy is enabled.
- `EXTERNAL_DATA_MODE=placeholder`: skip real external-data loading and rely only on compatibility tables.

Import and reimport payloads may provide `external_data_mode`, `external_data_uri`, and `external_data_source_mode` per job. This restores the useful upstream external-data behavior while keeping air-gapped deployments explicit and policy-controlled.

A dedicated `external-data/load` admin job is also available for cases where the OSM base tables are already present and only the Carto water, ice, or Natural Earth tables need to be loaded or refreshed.

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
- Import downloads are disabled unless an operator selects `source_mode=internal` or `source_mode=public` and enables the matching policy variable.
- External-data downloads are disabled unless an operator selects `external_data_source_mode=internal` or `external_data_source_mode=public` and enables the matching policy variable.

An airgap release normally contains:

- Role image tarballs.
- Docker Compose files or Kubernetes/Helm manifests.
- Map bundles and checksums.
- Local PBF, poly, and change files.
- Internal object-storage or filesystem configuration.

Fully offline image builds are not required by this design. Runtime operation after image delivery is offline-capable.

When teams want to pre-bake external-data archives into an image during a connected build, they can either run `run.sh prepare-external-data --style-dir ...` in a derived Dockerfile or enable the built-in Dockerfile build args:

- `PREPARE_EXTERNAL_DATA_AT_BUILD=true`
- `FETCH_EXTERNAL_DATA_AT_BUILD=true`
- `EXTERNAL_DATA_BUILD_SOURCE_MODE=public|internal|local`
- optional `EXTERNAL_DATA_BUILD_URI`

That writes vendored external-data files and `external-data.offline.yml` into the style directory without requiring a live PostGIS connection during the build, so later bundle generation can stay offline.

## Deployment Topologies

### Docker Compose With Filesystem Tiles

Use `docker-compose.yml` for a local external-PostGIS topology and a named filesystem tile cache. This mode starts two render workers by default against the same shared tile cache. It is useful for development and small self-hosted installs. It does not mount `examples/map-bundle` by default; import jobs use the image-bundled Carto Lua/style/index files unless a real bundle is mounted and `MAP_BUNDLE_URI` is set.

The Compose file keeps one worker process per renderer container and relies on multiple containers for concurrency. This is a good default for local Docker because worker identity, CPU scheduling, and logs stay easy to follow. Larger deployments can add more renderer replicas, raise `RENDER_WORKER_PROCESSES`, or do both after checking PostgreSQL headroom and storage latency.

### Docker Compose With S3-Compatible Tiles

Use `deploy/docker-compose.s3.yml` for MinIO-backed tile storage. The same settings map to OCI Object Storage or another S3-compatible service. Like the filesystem Compose file, it uses the image-bundled Carto import assets by default and starts two render workers so object-storage-backed queue throughput is easy to test locally.

### Docker Compose For Airgapped Local Operation

Use `deploy/docker-compose.airgap.yml` when runtime network access should stay disabled by policy. It mounts a local offline bundle at `/data/bundles/default`, restricts import and external-data modes to `local`, and expects PBF or poly files to be staged under `../data/import`.

This is the clearest Compose example for an isolated environment because it makes the local-only assumptions explicit instead of relying on defaults.

### Docker Compose With Build-Time External-Data Prep

Use `deploy/docker-compose.external-data-build.yml` in a connected build environment when you want the image itself to carry vendored Carto external-data archives and an `external-data.offline.yml` file before the runtime ever starts.

This example passes the Docker build args that enable `prepare-external-data` and `--fetch` during image build. The resulting image is useful when later bundle generation or external-data loading must remain offline.

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

The Dockerfile builds one combined Debian trixie slim runtime that is reused by all targets. Role targets only set the default role command; they do not remove packages or create role-specific dependency sets. This keeps API, renderer, admin-worker, admin-ui, and final images operationally interchangeable.

Native geospatial and database packages come from Debian packages built for the runtime Python version:

- Mapnik and `mapnik-utils`.
- `osm2pgsql` and `osmium-tool`.
- PostgreSQL client tools.
- Python bindings supplied by Debian where they depend on native libraries, including Mapnik, psycopg2, and Shapely.

Python-only application dependencies are declared in `pyproject.toml` and pinned in `poetry.lock`. Poetry is a build-time tool only: the image build exports the locked main dependency set to a temporary requirements file, installs it into `/opt/tile-server-venv`, and discards Poetry before the runtime stage. The venv is created with `python3 -m venv --system-site-packages`, so Python packages installed from Poetry can coexist with Debian's native Python bindings. Poetry itself is not installed in the final runtime image.

This keeps the Dockerfile focused on operating system capabilities while `pyproject.toml` is the source of truth for Python SDK/client libraries such as S3 access and HTTP/YAML helpers.

## Compatibility

Legacy commands still route to the new role commands:

- `run` starts `tile-api`.
- `import` runs `import-once` against external PostgreSQL.

They no longer start bundled PostgreSQL, Apache, nginx, cron, or renderd.

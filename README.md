# OpenTilesX

Cloud-native, airgap-capable raster map tile server containers.

Inspired by the original openstreetmap-tile-server, this project rearchitects the tile server into separate role-based containers for API, rendering, and admin planes. It uses external PostgreSQL/PostGIS as the authoritative data source and supports both filesystem and S3-compatible tile storage. The design also emphasizes airgap operation with explicit import sources and offline map bundles.

This repository separates the tile serving, rendering, and admin/import/update planes. PostgreSQL/PostGIS is external and authoritative. Rendered tiles can be stored on a shared filesystem or in S3-compatible object storage, including OCI Object Storage.

## Architecture

See [docs/architecture.md](docs/architecture.md) for the full design, including role boundaries, storage adapters, map bundles, airgap operation, imports/updates, overlays, and deployment topologies.

The image exposes role targets:

- `api`: serves `GET /tile/{z}/{x}/{y}.png` and `GET /tile/{layer}/{z}/{x}/{y}.png`, reads rendered tiles from the configured tile store, and enqueues missing tiles.
- `renderer`: consumes dirty tile records, renders Mapnik PNG tiles from external PostGIS, and writes them to the configured tile store.
- `admin-worker`: owns imports, updates, reimports, map-bundle activation, and tile expiry jobs.
- `admin-ui`: same runtime as `api`, with admin endpoints enabled by configuration.

The old `run` and `import` commands remain as compatibility shims:

- `run` starts `tile-api`.
- `import` runs a one-shot external-database import.

They no longer start PostgreSQL, Apache, nginx, cron, or renderd inside the tile server container.

## Required Configuration

Database:

```sh
RENDER_DATABASE_URL=postgresql://render:secret@postgres.internal:5432/gis
IMPORT_DATABASE_URL=postgresql://import:secret@postgres.internal:5432/gis
CONTROL_DATABASE_URL=postgresql://control:secret@postgres.internal:5432/gis
DATABASE_ADMIN_URL=postgresql://admin:secret@postgres.internal:5432/gis
```

`RENDER_DATABASE_URL` is used by the tile API and renderer. `IMPORT_DATABASE_URL` is used by import/update jobs. `CONTROL_DATABASE_URL` is optional; if omitted, metadata jobs use `IMPORT_DATABASE_URL` or `RENDER_DATABASE_URL`. `DATABASE_ADMIN_URL` is only for bootstrap.

Bootstrap metadata tables once:

```sh
docker run --rm \
  -e IMPORT_DATABASE_URL=postgresql://import:secret@postgres.internal:5432/gis \
  ghcr.io/example/opentilesx-admin-worker:latest \
  bootstrap
```

When `DATABASE_ADMIN_URL` is provided, bootstrap also creates the required `postgis` and `hstore` extensions if they are missing.

## Tile Storage

Filesystem:

```sh
TILE_STORE=filesystem
TILE_FS_PATH=/data/tiles
```

For Kubernetes with more than one API or renderer pod, use a `ReadWriteMany` volume.

S3-compatible object storage:

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

The runtime uses object API calls directly. It does not mount object storage with `s3fs`.

## Airgap Runtime

Runtime imports never download a public sample PBF, style asset, font, favicon, Leaflet bundle, or replication feed implicitly. Isolated deployments must provide inputs from local paths, internal object storage, or internal HTTP mirrors.

Import sources are explicit:

```sh
IMPORT_SOURCE_MODE=local
ALLOW_INTERNAL_IMPORT_DOWNLOADS=false
ALLOW_PUBLIC_IMPORT_DOWNLOADS=false
EXTERNAL_DATA_MODE=auto
ALLOW_INTERNAL_EXTERNAL_DATA_DOWNLOADS=false
ALLOW_PUBLIC_EXTERNAL_DATA_DOWNLOADS=false
```

- `local`: mounted paths or `file://` only. This is the default and works with `/data/region.osm.pbf`, optional `/data/region.poly`, `PBF_URI`/`PBF_PATH`, and `POLY_URI`/`POLY_PATH`.
- `internal`: internal `http(s)://`, `s3://`, or `oci://` inputs when `ALLOW_INTERNAL_IMPORT_DOWNLOADS=true`.
- `public`: explicit public `http(s)://` inputs when `ALLOW_PUBLIC_IMPORT_DOWNLOADS=true`.

The upstream `DOWNLOAD_PBF` and `DOWNLOAD_POLY` variables are accepted as explicit input URIs, but there is no silent Luxembourg fallback. Downloaded inputs are staged under `/tmp/tile-server/imports/<job-id>/` with size and SHA-256 metadata in the job result.

OpenStreetMap Carto external data is explicit too:

- `EXTERNAL_DATA_MODE=auto`: use vendored bundle data or a staged local preload when present, otherwise fall back to placeholder compatibility tables.
- `EXTERNAL_DATA_MODE=local`: require vendored or preloaded offline assets and load them with `get-external-data.py` using a generated offline config.
- `EXTERNAL_DATA_MODE=fetch`: run the upstream-style `get-external-data.py` fetch path only when the matching internal/public download policy is enabled.
- `EXTERNAL_DATA_MODE=placeholder`: skip real external-data loading and rely on empty compatibility tables for air-gapped quick starts.

Import and reimport jobs also accept `external_data_mode`, `external_data_uri`, and `external_data_source_mode`. That lets operators preload water, ice, or Natural Earth archives from a local directory, mounted archive, internal mirror, S3, or OCI object storage without reintroducing silent public downloads.

If the base OSM data is already imported, you can refresh only the external-data tables with a dedicated admin job instead of rerunning `osm2pgsql`.

Map-specific assets are packaged as an offline map bundle:

```sh
MAP_BUNDLE_URI=file:///data/bundles/default
```

A bundle contains:

- `manifest.json`, `manifest.yaml`, or `map-bundle.yaml`
- prebuilt `style.mapnikXml`
- osm2pgsql Lua/style/index SQL files
- fonts, symbols, sprites, and external data
- optional PBF/poly references
- baked overlay definitions and optional separate overlay layers

Separate overlay layers can define their own `layers[].style.mapnikXml`; otherwise they fall back to the bundle default style.

`map-bundle/activate` records the active bundle and checksum in `tile_admin.settings`. New API/renderer pods use that active bundle if `MAP_BUNDLE_URI` is not set explicitly.

Validate a bundle:

```sh
docker run --rm \
  -v "$PWD/examples/map-bundle:/data/bundles/default:ro" \
  ghcr.io/example/opentilesx-api:latest \
  validate-bundle file:///data/bundles/default
```

HTTP bundle URLs are disabled by default. Set `ALLOW_NETWORK_FETCH=true` only when pointing at an approved internal mirror.

Generate a bundle from a prepared style directory:

```sh
docker run --rm \
  -v "$PWD/data/import:/data/import:ro" \
  -v "$PWD/data/bundles:/data/bundles" \
  ghcr.io/example/opentilesx-api:latest \
  generate-bundle \
    --style-dir /opt/openstreetmap-carto-default \
    --output /data/bundles/malaysia-2026.04.tar.gz \
    --name malaysia \
    --version 2026.04 \
    --pbf /data/import/region.osm.pbf \
    --poly /data/import/region.poly
```

Use `--fetch-external-data` only in a connected environment with `IMPORT_SOURCE_MODE=internal` and `ALLOW_INTERNAL_IMPORT_DOWNLOADS=true`, or with the explicit public-download policy enabled. The generator copies the style, optional PBF/poly, manifest, bundled external data, and checksum file into a `.tar.gz` that can be moved into an air-gapped runtime.

To vendor external data without public fetches, point the generator at a prepared local directory or archive:

```sh
docker run --rm \
  -v "$PWD/data/preloaded-external:/data/preloaded-external:ro" \
  -v "$PWD/data/bundles:/data/bundles" \
  ghcr.io/example/opentilesx-api:latest \
  generate-bundle \
    --style-dir /opt/openstreetmap-carto-default \
    --output /data/bundles/malaysia-offline.tar.gz \
    --name malaysia \
    --version 2026.04 \
    --external-data-uri /data/preloaded-external \
    --external-data-source-mode local
```

The bundle generator writes an `externalData` section into the manifest and creates `style/external-data.offline.yml` with `file://` URLs pointing at the vendored assets inside the bundle.

## Admin API

Admin endpoints are disabled by default. Enable them on the API role:

```sh
ADMIN_ENABLED=true
ADMIN_TOKEN=change-me
```

Admin mode also needs `CONTROL_DATABASE_URL` or `IMPORT_DATABASE_URL` because it creates jobs.

When enabled, the browser control panel is available at `/admin`. It is self-contained and does not load external assets.

The offline map preview is available at `/map`. It uses no Leaflet, CDN, or third-party browser library; it only requests this server's `/tile/...png` endpoints so operators can test loaded tiles in isolated environments.

Render performance defaults favor cached map browsing:

```sh
WORKER_ID=render-worker-1
IMPORT_THREADS=4
THREADS=4
RENDER_BACKEND=python-mapnik
RENDER_BACKEND_URL=http://render-sidecar:9000
RENDER_WORKER_PROCESSES=1
RENDER_DB_MAX_ACTIVE=2
RENDER_DB_STATEMENT_TIMEOUT_MS=30000
METATILE_SIZE=8
STORE_FULL_METATILE=true
NEARBY_PREFETCH_ENABLED=true
NEARBY_PREFETCH_RADIUS=1
NEARBY_PREFETCH_PRIORITY=250
WORKER_BATCH_SIZE=8
```

`IMPORT_THREADS` is the preferred import tuning knob for `osm2pgsql`; `THREADS` remains as the backward-compatible alias. Tile rendering concurrency is separate from import concurrency. Start with multiple renderer containers and `RENDER_WORKER_PROCESSES=1` before raising per-container worker counts. `RENDER_DB_MAX_ACTIVE` is the hard safety cap for how many metatile renders may hit PostGIS at once across the worker fleet, and `RENDER_DB_STATEMENT_TIMEOUT_MS` injects a statement timeout plus read-only session options into Mapnik PostGIS datasource connections. `RENDER_BACKEND=python-mapnik` keeps rendering inside the worker process; `http-sidecar` is available for a future dedicated render service through `RENDER_BACKEND_URL`.

The renderer queues work per metatile, not per tile, so repeated misses within the same area are coalesced before rendering. One Mapnik metatile render can store every PNG in that metatile, which makes the first missing tile in an area slower but warms nearby tiles quickly. Lower `METATILE_SIZE` to reduce memory and initial latency; raise it carefully only after checking worker memory and S3/write throughput. For interactive browsing, the local `docker-compose.yml` quick start sets `METATILE_SIZE=4` so the first uncached tile waits on a smaller render. For S3-compatible storage, write latency can dominate render time, so scaling workers only helps when PostGIS and object storage can both keep up.

When `NEARBY_PREFETCH_ENABLED=true`, the API only fans out nearby work when a metatile miss is first seen. It queues the four orthogonal neighboring metatiles as lower-priority `nearby-prefetch` jobs, so direct misses still outrank speculative warm-up. `NEARBY_PREFETCH_RADIUS` expands that ring by additional orthogonal steps, and `NEARBY_PREFETCH_PRIORITY` controls the queue priority for that prefetch work.

The Compose quick start runs two render workers by default and keeps the cluster-wide PostGIS render cap at `RENDER_DB_MAX_ACTIVE=2`. It also uses the smaller 4x4 metatile preset with nearby ring prefetch enabled to improve first-tile responsiveness while still warming adjacent cache. Add more workers only when the host CPU, PostGIS, and tile storage can keep up, and do not raise `RENDER_DB_MAX_ACTIVE` until queue age and render latency stay healthy under load. Multiple workers coordinate through PostgreSQL leases, a render-slot advisory lock pool, and a per-metatile advisory lock so adjacent missing tiles do not waste time rendering the same metatile twice.

Create jobs:

```sh
curl -X POST http://localhost:8080/admin/jobs/import \
  -H 'Authorization: Bearer change-me' \
  -H 'Content-Type: application/json' \
  -d '{"pbf_uri":"/data/import/region.osm.pbf"}'

curl -X POST http://localhost:8080/admin/jobs/import \
  -H 'Authorization: Bearer change-me' \
  -H 'Content-Type: application/json' \
  -d '{"pbf_uri":"https://mirror.internal/osm/region.osm.pbf","poly_uri":"https://mirror.internal/osm/region.poly","source_mode":"internal"}'

curl -X POST http://localhost:8080/admin/jobs/reimport \
  -H 'Authorization: Bearer change-me' \
  -H 'Content-Type: application/json' \
  -d '{"pbf_uri":"/data/import/region.osm.pbf","replace_strategy":"blue_green"}'

curl -X POST http://localhost:8080/admin/jobs/update \
  -H 'Authorization: Bearer change-me' \
  -H 'Content-Type: application/json' \
  -d '{"change_uri":"/data/updates/latest.osc.gz"}'

curl -X POST http://localhost:8080/admin/jobs/external-data/load \
  -H 'Authorization: Bearer change-me' \
  -H 'Content-Type: application/json' \
  -d '{"style_dir":"/opt/openstreetmap-carto-default","external_data_mode":"local","external_data_uri":"/data/external-data/osm-carto-cache","external_data_source_mode":"local"}'

curl -H 'Authorization: Bearer change-me' \
  'http://localhost:8080/admin/jobs?limit=50'

curl -H 'Authorization: Bearer change-me' \
  http://localhost:8080/admin/jobs/{job-id}
```

`osm2pgsql --append` is only used for change files. Full `.osm.pbf` files use replace/reimport.

## Reimport Strategies

- `blue_green`: import into a new schema, validate, then update the active schema metadata. This is the default for lower downtime.
- `in_place`: recreate active OSM tables directly. This uses less storage but can degrade or interrupt rendering while the job runs.

## Local Compose

Filesystem tile cache:

```sh
docker compose up --build
```

This starts `render-worker` and `render-worker-2` against the same external PostGIS database and shared `tile-cache` volume.

S3-compatible cache using MinIO:

```sh
docker compose -f deploy/docker-compose.s3.yml up --build
```

Airgapped local bundle plus local imports only:

```sh
docker compose -f deploy/docker-compose.airgap.yml up --build
```

Connected build that pre-bakes Carto external-data cache into the image:

```sh
docker compose -f deploy/docker-compose.external-data-build.yml build
docker compose -f deploy/docker-compose.external-data-build.yml up
```

Compose example guide:

- [docker-compose.yml](/Users/yeekit/Desktop/My%20Projects/Untitled/openstreetmap-tile-server/docker-compose.yml): local filesystem tiles, two render workers, image-bundled Carto assets.
- [deploy/docker-compose.s3.yml](/Users/yeekit/Desktop/My%20Projects/Untitled/openstreetmap-tile-server/deploy/docker-compose.s3.yml): MinIO or other S3-compatible tile storage, two render workers.
- [deploy/docker-compose.airgap.yml](/Users/yeekit/Desktop/My%20Projects/Untitled/openstreetmap-tile-server/deploy/docker-compose.airgap.yml): local-only imports plus mounted offline bundle for isolated environments.
- [deploy/docker-compose.external-data-build.yml](/Users/yeekit/Desktop/My%20Projects/Untitled/openstreetmap-tile-server/deploy/docker-compose.external-data-build.yml): build images with vendored external-data cache already baked in.

Place offline import files under `./data/import` before creating import jobs. The Compose examples use the image-bundled OpenStreetMap Carto import assets through `NAME_LUA`, `NAME_STYLE`, and `NAME_INDEXES`, so a local import job only needs the PBF path.

The checked-in `examples/map-bundle` is a structural offline-bundle example. Its Carto import assets are placeholders, so real imports must either use the image-bundled Carto defaults or mount/activate a real bundle that contains valid `openstreetmap-carto.lua`, `openstreetmap-carto.style`, `indexes.sql`, Mapnik XML, fonts, and any external data needed by that style. The admin worker rejects placeholder bundle assets before invoking `osm2pgsql`.

## External-Data Cache Prep

To create a local external-data cache without touching PostGIS, run:

```sh
docker run --rm \
  -v "$PWD/data/external-data:/data/external-data" \
  opentilesx-final:local \
  prepare-external-data \
    --style-dir /opt/openstreetmap-carto-default \
    --external-data-uri /data/external-data/osm-carto-cache \
    --source-mode local
```

That writes vendored files into the style `data/` directory and generates `external-data.offline.yml` beside the style so later `external-data/load`, `import`, or `reimport` jobs can stay offline.

The main Dockerfile can also prefetch and vendor the cache during image build:

```sh
docker build \
  --target final \
  -t opentilesx-final:with-external-data \
  --build-arg PREPARE_EXTERNAL_DATA_AT_BUILD=true \
  --build-arg FETCH_EXTERNAL_DATA_AT_BUILD=true \
  --build-arg EXTERNAL_DATA_BUILD_SOURCE_MODE=public \
  .
```

Useful build args:

- `PREPARE_EXTERNAL_DATA_AT_BUILD=true`: run the external-data prep step during image build.
- `FETCH_EXTERNAL_DATA_AT_BUILD=true`: actively download upstream external-data archives instead of only using an already vendored or preloaded cache.
- `EXTERNAL_DATA_BUILD_SOURCE_MODE=public|internal|local`: apply the matching policy mode during the build.
- `EXTERNAL_DATA_BUILD_URI=/path/or/uri`: optional preload directory or archive to vendor instead of fetching.
- `EXTERNAL_DATA_BUILD_STYLE_DIR=/opt/openstreetmap-carto-default`: optional alternate style directory inside the image.

After a successful build, `/opt/openstreetmap-carto-default/data` and `/opt/openstreetmap-carto-default/external-data.offline.yml` are already present in the image, so later `generate-bundle` runs can stay offline.

You can still do the same thing in a derived image in a connected build environment:

```dockerfile
FROM opentilesx-final:local
ENV ALLOW_PUBLIC_EXTERNAL_DATA_DOWNLOADS=true
RUN /run.sh prepare-external-data \
    --style-dir /opt/openstreetmap-carto-default \
    --source-mode public \
    --fetch
```

That derived image keeps the downloaded external-data archives and offline config locally, so runtime imports can use `external_data_mode=auto` or `local` without reaching the internet.

## Kubernetes and Helm

Static manifests are in `deploy/kubernetes/tile-server.yaml`.

Helm chart:

```sh
helm install osm-tiles deploy/helm/tile-server \
  --set mapBundle.existingClaim=osm-map-bundle \
  --set database.renderUrl='postgresql://render:secret@postgres.internal:5432/gis' \
  --set database.controlUrl='postgresql://control:secret@postgres.internal:5432/gis' \
  --set database.importUrl='postgresql://import:secret@postgres.internal:5432/gis'
```

Use S3 mode by setting `tileStore.type=s3` and the `tileStore.s3.*` values. For filesystem mode, provide or let the chart create a `ReadWriteMany` tile PVC.

Containers run as UID/GID `1000`; externally provisioned PVCs and bind mounts must be writable by that ID.

## Airgap Release Flow

1. Build and export role images in a connected environment:

   ```sh
   docker build --target api -t opentilesx-api:airgap .
   docker build --target renderer -t opentilesx-renderer:airgap .
   docker build --target admin-worker -t opentilesx-admin-worker:airgap .
   docker save opentilesx-api:airgap opentilesx-renderer:airgap opentilesx-admin-worker:airgap -o opentilesx-images.tar
   ```

2. Assemble a local map bundle inside the isolated environment if the map cannot leave it.
3. Transfer only approved artifacts into the isolated environment: image tarball, manifests/chart, PBF/poly/change files, map bundle, and checksums.
4. Load images, bootstrap metadata, validate the bundle, import local data, then start API and render workers.

## Development

Run unit tests:

```sh
python3 -m unittest discover -s tests
```

Build role images:

```sh
make build
```

The CI workflow builds `api`, `renderer`, `admin-worker`, and `admin-ui` targets for amd64 and arm64.

## Credits

- [openstreetmap-tile-server](https://github.com/Overv/openstreetmap-tile-server) for the original open-source tile server and ecosystem that inspired this project.

# openstreetmap-tile-server

Cloud-native, airgap-capable OpenStreetMap raster tile server containers.

This repository now separates the tile serving, rendering, and admin/import/update planes. PostgreSQL/PostGIS is external and authoritative. Rendered tiles can be stored on a shared filesystem or in S3-compatible object storage, including OCI Object Storage.

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
  ghcr.io/example/openstreetmap-tile-server-admin-worker:latest \
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
  ghcr.io/example/openstreetmap-tile-server-api:latest \
  validate-bundle file:///data/bundles/default
```

HTTP bundle URLs are disabled by default. Set `ALLOW_NETWORK_FETCH=true` only when pointing at an approved internal mirror.

## Admin API

Admin endpoints are disabled by default. Enable them on the API role:

```sh
ADMIN_ENABLED=true
ADMIN_TOKEN=change-me
```

Admin mode also needs `CONTROL_DATABASE_URL` or `IMPORT_DATABASE_URL` because it creates jobs.

When enabled, the browser control panel is available at `/admin`. It is self-contained and does not load external assets.

Create jobs:

```sh
curl -X POST http://localhost:8080/admin/jobs/import \
  -H 'Authorization: Bearer change-me' \
  -H 'Content-Type: application/json' \
  -d '{"pbf_uri":"/data/import/region.osm.pbf","lua":"/data/bundles/default/style/openstreetmap-carto.lua","style_file":"/data/bundles/default/style/openstreetmap-carto.style"}'

curl -X POST http://localhost:8080/admin/jobs/reimport \
  -H 'Authorization: Bearer change-me' \
  -H 'Content-Type: application/json' \
  -d '{"pbf_uri":"/data/import/region.osm.pbf","replace_strategy":"blue_green"}'

curl -X POST http://localhost:8080/admin/jobs/update \
  -H 'Authorization: Bearer change-me' \
  -H 'Content-Type: application/json' \
  -d '{"change_uri":"/data/updates/latest.osc.gz"}'

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

S3-compatible cache using MinIO:

```sh
docker compose -f deploy/docker-compose.s3.yml up --build
```

Place offline import files under `./data/import` before creating import jobs.

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
   docker build --target api -t osm-tile-api:airgap .
   docker build --target renderer -t osm-tile-renderer:airgap .
   docker build --target admin-worker -t osm-tile-admin-worker:airgap .
   docker save osm-tile-api:airgap osm-tile-renderer:airgap osm-tile-admin-worker:airgap -o osm-tile-images.tar
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

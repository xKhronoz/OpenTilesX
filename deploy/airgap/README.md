# Airgap Operation

Runtime is designed to work without internet access. The tile server will not silently download public PBF files, map assets, or replication feeds.

## Transfer Set

Prepare these artifacts before entering the isolated environment:

- role image tarballs for `api`, `renderer`, and `admin-worker`
- Kubernetes manifests or the Helm chart
- external PostgreSQL/PostGIS deployment or connection details
- PBF/poly files, or an internal change-file feed
- map bundle with style XML, Lua/style/index files, fonts, symbols, external data, and overlays
- checksums for every transferred artifact

## Custom Maps That Cannot Leave

Create or assemble the map bundle inside the isolated environment. The bundle is then mounted into the tile server roles or placed in internal object storage. Use `validate-bundle` before activation.

Overlays can be:

- baked into the main rendered PNG tiles after loading overlay data into PostGIS
- exposed as separate tile layers through `/tile/{layer}/{z}/{x}/{y}.png`

## Updates

Use one of two offline update models:

- Internal replication mirror: stage `.osc.gz` change files and run `update` jobs.
- Manual reimport: stage a new `.osm.pbf` and run a `reimport` job with `blue_green` or `in_place`.

Do not enable HTTP fetches unless the URL points to an approved internal mirror and `ALLOW_NETWORK_FETCH=true` is set intentionally.

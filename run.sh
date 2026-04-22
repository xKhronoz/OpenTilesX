#!/bin/bash

set -euo pipefail

if [ "$#" -lt 1 ]; then
    exec python3 -m tile_server.cli
fi

case "$1" in
    run)
        echo "WARNING: 'run' is deprecated; starting the cloud-native tile-api role." >&2
        shift
        exec python3 -m tile_server.cli tile-api "$@"
        ;;
    import)
        echo "WARNING: 'import' is deprecated; running import-once against external PostgreSQL." >&2
        shift
        exec python3 -m tile_server.cli import-once "$@"
        ;;
    tile-api|render-worker|admin-worker|bootstrap|import-once|update-once|validate-bundle|generate-bundle|prepare-external-data|healthcheck)
        exec python3 -m tile_server.cli "$@"
        ;;
    *)
        exec python3 -m tile_server.cli "$@"
        ;;
esac

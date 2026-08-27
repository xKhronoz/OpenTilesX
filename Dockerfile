# syntax=docker/dockerfile:1.7

ARG CARTO_VERSION=1.2.0
ARG DEBIAN_VERSION=trixie-slim
ARG OSM_CARTO_VERSION=v5.4.0
ARG PG_VERSION=17
ARG POETRY_EXPORT_PLUGIN_VERSION=1.9.0
ARG POETRY_VERSION=2.2.1
ARG PREPARE_EXTERNAL_DATA_AT_BUILD=false
ARG FETCH_EXTERNAL_DATA_AT_BUILD=false
ARG EXTERNAL_DATA_BUILD_SOURCE_MODE=public
ARG EXTERNAL_DATA_BUILD_URI=
ARG EXTERNAL_DATA_BUILD_STYLE_DIR=/opt/openstreetmap-carto-default

FROM debian:${DEBIAN_VERSION} AS builder-common

ARG CARTO_VERSION
ENV DEBIAN_FRONTEND=noninteractive \
  LANG=C.UTF-8 \
  LC_ALL=C.UTF-8

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
  --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
  set -eux; \
  apt-get update; \
  apt-get install -y --no-install-recommends \
  ca-certificates \
  curl \
  git \
  npm

RUN --mount=type=cache,target=/root/.npm \
  npm install -g "carto@${CARTO_VERSION}"

###########################################################################################################

FROM builder-common AS compiler-stylesheet

ARG OSM_CARTO_VERSION
WORKDIR /root/openstreetmap-carto

RUN git clone --single-branch --branch "$OSM_CARTO_VERSION" https://github.com/gravitystorm/openstreetmap-carto.git --depth 1 . \
  && sed -i 's/, "unifont Medium", "Unifont Upper Medium"//g' style/fonts.mss \
  && sed -i 's/"Noto Sans Tibetan Regular",//g' style/fonts.mss \
  && sed -i 's/"Noto Sans Tibetan Bold",//g' style/fonts.mss \
  && sed -i 's/Noto Sans Syriac Eastern Regular/Noto Sans Syriac Regular/g' style/fonts.mss \
  && carto project.mml > mapnik.xml \
  && rm -rf .git

###########################################################################################################

FROM builder-common AS compiler-helper-script

WORKDIR /home/renderer/src/regional

RUN git clone https://github.com/zverik/regional . \
  && rm -rf .git \
  && chmod u+x trim_osc.py

###########################################################################################################

FROM builder-common AS font-builder

WORKDIR /tmp/fonts

RUN curl -fL -o NotoEmoji-Regular.ttf "https://github.com/googlefonts/noto-emoji/blob/9a5261d871451f9b5183c93483cbd68ed916b1e9/fonts/NotoEmoji-Regular.ttf?raw=true" \
  && curl -fL -o unifont-Medium.ttf "https://github.com/stamen/terrain-classic/blob/master/fonts/unifont-Medium.ttf?raw=true"

###########################################################################################################

FROM debian:${DEBIAN_VERSION} AS python-deps

ARG POETRY_EXPORT_PLUGIN_VERSION
ARG POETRY_VERSION
ENV DEBIAN_FRONTEND=noninteractive \
  LANG=C.UTF-8 \
  LC_ALL=C.UTF-8 \
  VIRTUAL_ENV=/opt/tile-server-venv \
  PATH=/opt/tile-server-venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  POETRY_NO_INTERACTION=1

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
  --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
  set -eux; \
  apt-get update; \
  apt-get install -y --no-install-recommends \
  ca-certificates \
  python3-pip \
  python3-venv

WORKDIR /tmp/tile-server-deps
COPY pyproject.toml poetry.lock ./

RUN --mount=type=cache,target=/root/.cache/pip \
  --mount=type=cache,target=/root/.cache/pypoetry \
  set -eux; \
  python3 -m venv --system-site-packages "$VIRTUAL_ENV"; \
  python3 -m venv /tmp/poetry-venv; \
  /tmp/poetry-venv/bin/pip install \
  "poetry==${POETRY_VERSION}" \
  "poetry-plugin-export==${POETRY_EXPORT_PLUGIN_VERSION}"; \
  /tmp/poetry-venv/bin/poetry export \
  --only main \
  --format requirements.txt \
  --output /tmp/requirements.txt \
  --without-hashes; \
  "$VIRTUAL_ENV/bin/pip" install --requirement /tmp/requirements.txt; \
  "$VIRTUAL_ENV/bin/pip" uninstall -y pip setuptools wheel || true; \
  rm -rf "$VIRTUAL_ENV"/bin/pip* \
  "$VIRTUAL_ENV"/lib/python*/site-packages/pip \
  "$VIRTUAL_ENV"/lib/python*/site-packages/pip-*.dist-info \
  "$VIRTUAL_ENV"/lib/python*/site-packages/setuptools \
  "$VIRTUAL_ENV"/lib/python*/site-packages/setuptools-*.dist-info \
  "$VIRTUAL_ENV"/lib/python*/site-packages/wheel \
  "$VIRTUAL_ENV"/lib/python*/site-packages/wheel-*.dist-info; \
  rm -rf /tmp/poetry-venv

###########################################################################################################

FROM builder-common AS frontend-builder

WORKDIR /workspace
COPY package.json package-lock.json ./

RUN --mount=type=cache,target=/root/.npm \
  npm ci

COPY scripts scripts
COPY frontend/app frontend/app

RUN npm run build:app

###########################################################################################################

FROM debian:${DEBIAN_VERSION} AS runtime

ARG PG_VERSION
ARG PREPARE_EXTERNAL_DATA_AT_BUILD
ARG FETCH_EXTERNAL_DATA_AT_BUILD
ARG EXTERNAL_DATA_BUILD_SOURCE_MODE
ARG EXTERNAL_DATA_BUILD_URI
ARG EXTERNAL_DATA_BUILD_STYLE_DIR
ENV DEBIAN_FRONTEND=noninteractive \
  LANG=C.UTF-8 \
  LC_ALL=C.UTF-8 \
  VIRTUAL_ENV=/opt/tile-server-venv \
  PATH=/opt/tile-server-venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  PYTHONPATH=/app \
  TILE_STORE=filesystem \
  TILE_FS_PATH=/data/tiles \
  MAP_VERSION=default \
  DEFAULT_LAYER=default \
  LISTEN_HOST=0.0.0.0 \
  LISTEN_PORT=8080 \
  THREADS=4 \
  IMPORT_THREADS=4 \
  RENDER_WORKER_PROCESSES=1 \
  RENDER_BACKEND=python-mapnik \
  IMPORT_SOURCE_MODE=local \
  ALLOW_INTERNAL_IMPORT_DOWNLOADS=false \
  ALLOW_PUBLIC_IMPORT_DOWNLOADS=false \
  IMPORT_STAGING_PATH=/tmp/tile-server/imports \
  EXTERNAL_DATA_MODE=auto \
  ALLOW_INTERNAL_EXTERNAL_DATA_DOWNLOADS=false \
  ALLOW_PUBLIC_EXTERNAL_DATA_DOWNLOADS=false \
  EXTERNAL_DATA_STAGING_PATH=/tmp/tile-server/external-data \
  MAP_BUNDLE_OUTPUT_DIR=/data/bundles/generated \
  TZ=UTC

RUN ln -snf "/usr/share/zoneinfo/$TZ" /etc/localtime \
  && echo "$TZ" > /etc/timezone

# Runtime deliberately excludes PostgreSQL server, Apache, nginx, cron, renderd,
# sudo, vim, npm, and git. PostgreSQL/PostGIS is external; ingress/proxying
# belongs to deployment infrastructure.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
  --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
  set -eux; \
  apt-get update; \
  apt-get install -y --no-install-recommends \
  ca-certificates \
  fonts-hanazono \
  fonts-noto-cjk \
  fonts-noto-hinted \
  fonts-noto-unhinted \
  fonts-unifont \
  mapnik-utils \
  osm2pgsql \
  osmium-tool \
  postgresql-client-${PG_VERSION} \
  python3-mapnik \
  python3-psycopg2 \
  python3-shapely \
  tini

RUN adduser --uid 1000 --disabled-password --gecos "" renderer \
  && mkdir -p /data/tiles /data/style /data/import /data/bundles /tmp/tile-server /app \
  && chown -R renderer:renderer /data /tmp/tile-server /app /home/renderer

COPY --from=font-builder /tmp/fonts/NotoEmoji-Regular.ttf /usr/share/fonts/NotoEmoji-Regular.ttf
COPY --from=font-builder /tmp/fonts/unifont-Medium.ttf /usr/share/fonts/unifont-Medium.ttf
COPY --from=python-deps --chown=renderer:renderer /opt/tile-server-venv /opt/tile-server-venv
COPY --from=compiler-helper-script --chown=renderer:renderer /home/renderer/src/regional /home/renderer/src/regional
COPY --from=compiler-stylesheet --chown=renderer:renderer /root/openstreetmap-carto /opt/openstreetmap-carto-default
COPY --chown=renderer:renderer tile_server /app/tile_server
COPY --from=frontend-builder --chown=renderer:renderer /workspace/tile_server/static/web-ui /app/tile_server/static/web-ui
COPY --chown=renderer:renderer --chmod=755 run.sh /run.sh

RUN set -eux; \
  if [ "$PREPARE_EXTERNAL_DATA_AT_BUILD" = "true" ]; then \
  case "$EXTERNAL_DATA_BUILD_SOURCE_MODE" in \
  public) export ALLOW_PUBLIC_EXTERNAL_DATA_DOWNLOADS=true ;; \
  internal) export ALLOW_INTERNAL_EXTERNAL_DATA_DOWNLOADS=true ;; \
  local) ;; \
  *) echo "Unsupported EXTERNAL_DATA_BUILD_SOURCE_MODE: $EXTERNAL_DATA_BUILD_SOURCE_MODE" >&2; exit 1 ;; \
  esac; \
  if [ "$FETCH_EXTERNAL_DATA_AT_BUILD" = "true" ] && [ -n "$EXTERNAL_DATA_BUILD_URI" ]; then \
  python3 -m tile_server.cli prepare-external-data \
  --style-dir "$EXTERNAL_DATA_BUILD_STYLE_DIR" \
  --source-mode "$EXTERNAL_DATA_BUILD_SOURCE_MODE" \
  --external-data-uri "$EXTERNAL_DATA_BUILD_URI" \
  --fetch; \
  elif [ "$FETCH_EXTERNAL_DATA_AT_BUILD" = "true" ]; then \
  python3 -m tile_server.cli prepare-external-data \
  --style-dir "$EXTERNAL_DATA_BUILD_STYLE_DIR" \
  --source-mode "$EXTERNAL_DATA_BUILD_SOURCE_MODE" \
  --fetch; \
  elif [ -n "$EXTERNAL_DATA_BUILD_URI" ]; then \
  python3 -m tile_server.cli prepare-external-data \
  --style-dir "$EXTERNAL_DATA_BUILD_STYLE_DIR" \
  --source-mode "$EXTERNAL_DATA_BUILD_SOURCE_MODE" \
  --external-data-uri "$EXTERNAL_DATA_BUILD_URI"; \
  else \
  python3 -m tile_server.cli prepare-external-data \
  --style-dir "$EXTERNAL_DATA_BUILD_STYLE_DIR" \
  --source-mode "$EXTERNAL_DATA_BUILD_SOURCE_MODE"; \
  fi; \
  chown -R renderer:renderer "$EXTERNAL_DATA_BUILD_STYLE_DIR"; \
  fi

USER renderer
WORKDIR /app
ENTRYPOINT ["/usr/bin/tini", "--", "/run.sh"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD ["/run.sh", "healthcheck"]

FROM runtime AS api
ENV TILE_SERVER_ROLE=tile-api
CMD ["tile-api"]

FROM runtime AS renderer
ENV TILE_SERVER_ROLE=render-worker
CMD ["render-worker"]

FROM runtime AS admin-worker
ENV TILE_SERVER_ROLE=admin-worker
CMD ["admin-worker"]

FROM api AS admin-ui
ENV TILE_SERVER_ROLE=admin-ui

FROM api AS final

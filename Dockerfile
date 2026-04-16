# syntax=docker/dockerfile:1.7

ARG CARTO_VERSION=1.2.0
ARG OSM_CARTO_VERSION=v5.4.0
ARG PG_VERSION=15

FROM debian:bookworm-slim AS builder-common

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

FROM debian:bookworm-slim AS runtime

ARG PG_VERSION
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
    TZ=UTC

RUN ln -snf "/usr/share/zoneinfo/$TZ" /etc/localtime \
 && echo "$TZ" > /etc/timezone

# Runtime deliberately excludes PostgreSQL server, Apache, nginx, cron, renderd,
# sudo, vim, npm, and git. PostgreSQL/PostGIS is external; ingress/proxying
# belongs to deployment infrastructure.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    --mount=type=cache,target=/root/.cache/pip \
    set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        dateutils \
        fonts-hanazono \
        fonts-noto-cjk \
        fonts-noto-hinted \
        fonts-noto-unhinted \
        fonts-unifont \
        gdal-bin \
        liblua5.3-0 \
        lua5.3 \
        mapnik-utils \
        osm2pgsql \
        osmium-tool \
        osmosis \
        postgresql-client-${PG_VERSION} \
        python-is-python3 \
        python3-lxml \
        python3-mapnik \
        python3-pip \
        python3-psycopg2 \
        python3-shapely \
        python3-venv \
        tini; \
    python3 -m venv --system-site-packages "$VIRTUAL_ENV"; \
    "$VIRTUAL_ENV/bin/pip" install \
        boto3 \
        osmium \
        pyyaml \
        requests; \
    apt-get purge -y --auto-remove \
        python3-pip \
        python3-pip-whl \
        python3-setuptools \
        python3-setuptools-whl \
        python3-venv \
        python3-wheel

RUN adduser --uid 1000 --disabled-password --gecos "" renderer \
 && mkdir -p /data/tiles /data/style /data/import /data/bundles /tmp/tile-server /app \
 && chown -R renderer:renderer /data /tmp/tile-server /app /home/renderer

COPY --from=font-builder /tmp/fonts/NotoEmoji-Regular.ttf /usr/share/fonts/NotoEmoji-Regular.ttf
COPY --from=font-builder /tmp/fonts/unifont-Medium.ttf /usr/share/fonts/unifont-Medium.ttf
COPY --from=compiler-helper-script --chown=renderer:renderer /home/renderer/src/regional /home/renderer/src/regional
COPY --from=compiler-stylesheet --chown=renderer:renderer /root/openstreetmap-carto /opt/openstreetmap-carto-default
COPY --chown=renderer:renderer tile_server /app/tile_server
COPY --chown=renderer:renderer --chmod=755 run.sh /run.sh

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

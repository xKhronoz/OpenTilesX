from tile_server.config import AppConfig


def minimal_config(**overrides):
    values = {
        "role": "test",
        "render_database_url": "postgresql://render:secret@postgres:5432/gis",
        "import_database_url": "postgresql://import:secret@postgres:5432/gis",
        "control_database_url": None,
        "database_admin_url": None,
        "tile_store": "filesystem",
        "tile_fs_path": "/tmp/tiles",
        "s3": None,
        "map_version": "default",
        "default_layer": "default",
        "style_xml": None,
        "style_workdir": "/tmp/tile-style",
        "map_bundle_uri": None,
        "admin_enabled": False,
        "admin_token": None,
        "listen_host": "127.0.0.1",
        "listen_port": 8080,
        "worker_poll_interval": 0.1,
        "worker_batch_size": 8,
        "threads": 4,
        "osm2pgsql_extra_args": "",
        "allow_network_fetch": False,
        "dry_run": True,
    }
    values.update(overrides)
    return AppConfig(**values)

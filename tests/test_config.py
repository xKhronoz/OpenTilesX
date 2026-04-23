import os
import unittest
from unittest.mock import patch

from tile_server.config import AppConfig, ConfigError


class ConfigTests(unittest.TestCase):
    def test_tile_api_requires_external_render_database(self):
        with patch.dict(os.environ, {"TILE_STORE": "filesystem"}, clear=True):
            with self.assertRaises(ConfigError):
                AppConfig.from_env("tile-api")

    def test_filesystem_tile_api_config(self):
        env = {
            "RENDER_DATABASE_URL": "postgresql://render:secret@postgres/gis",
            "TILE_STORE": "filesystem",
            "TILE_FS_PATH": "/tiles",
        }
        with patch.dict(os.environ, env, clear=True):
            config = AppConfig.from_env("tile-api")
        self.assertEqual(config.tile_store, "filesystem")
        self.assertEqual(config.tile_fs_path, "/tiles")
        self.assertEqual(config.job_database_url, env["RENDER_DATABASE_URL"])

    def test_s3_requires_bucket(self):
        env = {
            "RENDER_DATABASE_URL": "postgresql://render:secret@postgres/gis",
            "TILE_STORE": "s3",
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ConfigError):
                AppConfig.from_env("tile-api")

    def test_admin_requires_token_when_enabled(self):
        env = {
            "RENDER_DATABASE_URL": "postgresql://render:secret@postgres/gis",
            "ADMIN_ENABLED": "true",
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ConfigError):
                AppConfig.from_env("tile-api")

    def test_metatile_size_is_configurable(self):
        env = {
            "RENDER_DATABASE_URL": "postgresql://render:secret@postgres/gis",
            "WORKER_ID": "renderer-a",
            "METATILE_SIZE": "4",
            "STORE_FULL_METATILE": "false",
            "IMPORT_THREADS": "6",
        }
        with patch.dict(os.environ, env, clear=True):
            config = AppConfig.from_env("render-worker")
        self.assertEqual(config.worker_id, "renderer-a")
        self.assertEqual(config.metatile_size, 4)
        self.assertFalse(config.store_full_metatile)
        self.assertEqual(config.import_threads, 6)

    def test_metatile_size_rejects_unsafe_values(self):
        env = {
            "RENDER_DATABASE_URL": "postgresql://render:secret@postgres/gis",
            "METATILE_SIZE": "32",
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ConfigError):
                AppConfig.from_env("render-worker")

    def test_nearby_prefetch_settings_are_configurable(self):
        env = {
            "RENDER_DATABASE_URL": "postgresql://render:secret@postgres/gis",
            "NEARBY_PREFETCH_ENABLED": "false",
            "NEARBY_PREFETCH_RADIUS": "2",
            "NEARBY_PREFETCH_PRIORITY": "275",
        }
        with patch.dict(os.environ, env, clear=True):
            config = AppConfig.from_env("tile-api")
        self.assertFalse(config.nearby_prefetch_enabled)
        self.assertEqual(config.nearby_prefetch_radius, 2)
        self.assertEqual(config.nearby_prefetch_priority, 275)

    def test_nearby_prefetch_rejects_invalid_values(self):
        env = {
            "RENDER_DATABASE_URL": "postgresql://render:secret@postgres/gis",
            "NEARBY_PREFETCH_RADIUS": "-1",
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ConfigError):
                AppConfig.from_env("tile-api")

    def test_http_sidecar_backend_requires_url(self):
        env = {
            "RENDER_DATABASE_URL": "postgresql://render:secret@postgres/gis",
            "RENDER_BACKEND": "http-sidecar",
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ConfigError):
                AppConfig.from_env("render-worker")


if __name__ == "__main__":
    unittest.main()

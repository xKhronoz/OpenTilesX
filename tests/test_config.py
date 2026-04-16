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


if __name__ == "__main__":
    unittest.main()

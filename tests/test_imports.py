import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import minimal_config
from tile_server.imports import ImportInputError, ImportInputResolver


class ImportInputResolverTests(unittest.TestCase):
    def test_local_pbf_and_poly_are_recorded_without_staging(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pbf = root / "region.osm.pbf"
            poly = root / "region.poly"
            pbf.write_bytes(b"pbf")
            poly.write_text("poly")

            payload, result = ImportInputResolver(minimal_config()).resolve_payload(
                {"pbf_uri": str(pbf), "poly_uri": str(poly)}, "job-1"
            )

        self.assertTrue(payload["pbf_uri"].endswith("region.osm.pbf"))
        self.assertTrue(payload["poly_uri"].endswith("region.poly"))
        self.assertFalse(result["inputs"]["pbf"]["staged"])
        self.assertFalse(result["inputs"]["poly"]["staged"])
        self.assertEqual(result["source_mode"], "local")

    def test_local_mode_rejects_network_input(self):
        resolver = ImportInputResolver(minimal_config())
        with self.assertRaisesRegex(ImportInputError, "source_mode=local"):
            resolver.resolve_payload({"pbf_uri": "https://example.test/region.osm.pbf"}, "job-1")

    def test_internal_download_requires_policy_flag(self):
        resolver = ImportInputResolver(minimal_config(import_source_mode="internal"))
        with self.assertRaisesRegex(ImportInputError, "ALLOW_INTERNAL_IMPORT_DOWNLOADS"):
            resolver.resolve_payload({"pbf_uri": "https://mirror.internal/region.osm.pbf"}, "job-1")

    def test_internal_download_stages_file_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = minimal_config(
                import_source_mode="internal",
                allow_internal_import_downloads=True,
                import_staging_path=tmp,
            )

            def write_file(uri, target):
                Path(target).write_bytes(b"downloaded")

            with mock.patch("tile_server.imports.urllib.request.urlretrieve", side_effect=write_file):
                payload, result = ImportInputResolver(config).resolve_payload(
                    {"pbf_uri": "https://mirror.internal/region.osm.pbf"}, "job-1"
                )

        self.assertTrue(payload["pbf_uri"].endswith("job-1/pbf.osm.pbf"))
        self.assertTrue(result["inputs"]["pbf"]["staged"])
        self.assertEqual(result["inputs"]["pbf"]["size"], len(b"downloaded"))

    def test_public_download_requires_explicit_policy_flag(self):
        resolver = ImportInputResolver(minimal_config(import_source_mode="public"))
        with self.assertRaisesRegex(ImportInputError, "ALLOW_PUBLIC_IMPORT_DOWNLOADS"):
            resolver.resolve_payload({"pbf_uri": "https://download.example/region.osm.pbf"}, "job-1")

    def test_checksum_mismatch_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            pbf = Path(tmp) / "region.osm.pbf"
            pbf.write_bytes(b"pbf")
            resolver = ImportInputResolver(minimal_config())
            with self.assertRaisesRegex(ImportInputError, "checksum mismatch"):
                resolver.resolve_payload({"pbf_uri": str(pbf), "expected_sha256": "0" * 64}, "job-1")


class ImportEnvCompatibilityTests(unittest.TestCase):
    def test_download_pbf_and_poly_envs_are_payload_sources(self):
        from tile_server import cli

        env = {
            "DOWNLOAD_PBF": "https://download.example/region.osm.pbf",
            "DOWNLOAD_POLY": "https://download.example/region.poly",
            "IMPORT_SOURCE_MODE": "public",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            payload = cli._payload_from_env(default_pbf="/definitely/not/present.osm.pbf")

        self.assertEqual(payload["pbf_uri"], env["DOWNLOAD_PBF"])
        self.assertEqual(payload["poly_uri"], env["DOWNLOAD_POLY"])
        self.assertEqual(payload["source_mode"], "public")


if __name__ == "__main__":
    unittest.main()

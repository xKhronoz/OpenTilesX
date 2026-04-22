import json
import tempfile
import unittest
from pathlib import Path

from tests.helpers import minimal_config
from tile_server.external_data import ExternalDataManager, ExternalDataError


def _write_external_style(style: Path, url: str = "https://example.test/water.zip") -> None:
    (style / "scripts").mkdir(parents=True, exist_ok=True)
    (style / "scripts" / "get-external-data.py").write_text("#!/usr/bin/env python3\n")
    (style / "external-data.yml").write_text(
        json.dumps(
            {
                "settings": {"data_dir": "data", "database": "gis"},
                "sources": {"water": {"url": url, "type": "shp"}},
            }
        )
    )


class ExternalDataTests(unittest.TestCase):
    def test_prepare_bundle_can_vendor_local_preload_and_write_offline_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            style = root / "style"
            preload = root / "preload"
            preload.mkdir()
            (preload / "water.zip").write_bytes(b"zip")
            _write_external_style(style)

            result = ExternalDataManager(minimal_config()).prepare_bundle(
                style,
                source_mode="local",
                external_data_uri=str(preload),
            )

            self.assertTrue(result["available"])
            self.assertEqual(result["mode"], "local-preload")
            self.assertTrue((style / "external-data.offline.yml").is_file())
            self.assertTrue((style / "data" / "water.zip").is_file())
            self.assertEqual(result["sources"][0]["name"], "water")

    def test_load_auto_falls_back_to_placeholder_when_no_local_data_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            style = Path(tmp) / "style"
            _write_external_style(style)

            result = ExternalDataManager(minimal_config()).load({}, style, job_id="job-1", dry_run=True)

        self.assertEqual(result["status"], "placeholder")
        self.assertEqual(result["fallback"], "placeholder")

    def test_load_local_uses_preloaded_external_data_in_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            style = root / "style"
            preload = root / "preload"
            preload.mkdir()
            (preload / "water.zip").write_bytes(b"zip")
            _write_external_style(style)
            config = minimal_config(external_data_staging_path=str(root / "staging"))

            result = ExternalDataManager(config).load(
                {
                    "external_data_mode": "local",
                    "external_data_uri": str(preload),
                    "external_data_source_mode": "local",
                },
                style,
                job_id="job-1",
                dry_run=True,
            )

        self.assertEqual(result["status"], "planned-local")
        self.assertIn("external-data.offline.yml", result["offline_config"])

    def test_fetch_requires_explicit_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            style = Path(tmp) / "style"
            _write_external_style(style)

            with self.assertRaisesRegex(ExternalDataError, "ALLOW_PUBLIC_EXTERNAL_DATA_DOWNLOADS"):
                ExternalDataManager(minimal_config()).load(
                    {
                        "external_data_mode": "fetch",
                        "external_data_source_mode": "public",
                    },
                    style,
                    job_id="job-1",
                    dry_run=True,
                )


if __name__ == "__main__":
    unittest.main()

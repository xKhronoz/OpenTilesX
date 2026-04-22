import json
import tarfile
import tempfile
import unittest
from pathlib import Path

from tests.helpers import minimal_config
from tile_server.bundle_generator import generate_map_bundle
from tile_server.bundles import MapBundleValidator


class BundleTests(unittest.TestCase):
    def test_valid_offline_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "style").mkdir()
            (root / "style" / "mapnik.xml").write_text("<Map></Map>")
            (root / "manifest.json").write_text(
                json.dumps(
                    {
                        "apiVersion": "tileserver.openstreetmap.local/v1",
                        "name": "isolated-map",
                        "version": "2026.04",
                        "style": {"directory": "style", "mapnikXml": "mapnik.xml"},
                        "layers": [{"name": "default"}, {"name": "overlay"}],
                    }
                )
            )
            result = MapBundleValidator(minimal_config()).validate(str(root))
        self.assertTrue(result.valid, result.errors)

    def test_bundle_requires_prebuilt_mapnik_xml(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "manifest.json").write_text(json.dumps({"name": "bad", "version": "1", "style": {}}))
            result = MapBundleValidator(minimal_config()).validate(str(root))
        self.assertFalse(result.valid)
        self.assertIn("style.mapnikXml", " ".join(result.errors))

    def test_external_data_manifest_without_data_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "style").mkdir()
            (root / "style" / "mapnik.xml").write_text("<Map></Map>")
            (root / "style" / "external-data.yml").write_text("layers: []")
            (root / "manifest.json").write_text(
                json.dumps(
                    {
                        "name": "bundle",
                        "version": "1",
                        "style": {"directory": "style", "mapnikXml": "mapnik.xml"},
                        "externalData": {"config": "style/external-data.yml"},
                    }
                )
            )
            result = MapBundleValidator(minimal_config()).validate(str(root))
        self.assertTrue(result.valid)
        self.assertIn("external data", " ".join(result.warnings))

    def test_generate_bundle_creates_archive_manifest_and_checksum(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            style = root / "style-src"
            style.mkdir()
            (style / "mapnik.xml").write_text("<Map></Map>")
            (style / "openstreetmap-carto.lua").write_text("-- lua")
            (style / "openstreetmap-carto.style").write_text("# style")
            (style / "indexes.sql").write_text("-- sql")
            pbf = root / "region.osm.pbf"
            poly = root / "region.poly"
            pbf.write_bytes(b"pbf")
            poly.write_text("poly")
            output = root / "bundle.tar.gz"

            result = generate_map_bundle(
                minimal_config(bundle_output_dir=str(root)),
                {
                    "style_dir": str(style),
                    "output": str(output),
                    "name": "test-map",
                    "version": "2026.04",
                    "pbf_uri": str(pbf),
                    "poly_uri": str(poly),
                },
            )

            with tarfile.open(output) as archive:
                names = set(archive.getnames())

            self.assertEqual(result["manifest"]["imports"]["pbf"], "data/region.osm.pbf")
            self.assertIn("externalData", result["manifest"])
            self.assertTrue(Path(result["checksum_file"]).is_file())
            self.assertIn("manifest.json", names)
            self.assertIn("style/mapnik.xml", names)
            self.assertIn("data/region.osm.pbf", names)


if __name__ == "__main__":
    unittest.main()

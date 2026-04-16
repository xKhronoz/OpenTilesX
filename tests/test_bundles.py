import json
import tempfile
import unittest
from pathlib import Path

from tests.helpers import minimal_config
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


if __name__ == "__main__":
    unittest.main()

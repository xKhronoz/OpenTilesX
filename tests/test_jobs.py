import unittest
import json
import tempfile
from pathlib import Path

from tests.helpers import minimal_config
from tile_server.jobs import CommandBuilder, JobError


class JobCommandTests(unittest.TestCase):
    def test_import_command_uses_external_database(self):
        config = minimal_config(import_database_url="postgresql://import:secret@db:5432/gis")
        command = CommandBuilder(config).import_command({"pbf_uri": "/data/import/region.osm.pbf"})
        self.assertIn("--create", command)
        self.assertIn("-d", command)
        self.assertIn("postgresql://import:secret@db:5432/gis", command)

    def test_append_rejects_full_pbf(self):
        config = minimal_config()
        with self.assertRaises(JobError):
            CommandBuilder(config).import_command({"pbf_uri": "/data/import/region.osm.pbf"}, append=True)

    def test_update_command_accepts_change_file_and_expiry_output(self):
        config = minimal_config()
        command = CommandBuilder(config).update_command(
            {"change_uri": "/data/updates/001.osc.gz", "expiry_file": "/tmp/dirty_tiles"}
        )
        self.assertIn("--append", command)
        self.assertIn("-e13-20", command)
        self.assertIn("-o", command)
        self.assertIn("/tmp/dirty_tiles", command)

    def test_masked_command_hides_database_password(self):
        command = ["osm2pgsql", "-d", "postgresql://user:secret@db/gis"]
        masked = CommandBuilder.masked(command)
        self.assertEqual(masked[-1], "postgresql://***@db/gis")

    def test_import_command_can_use_bundle_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "style").mkdir()
            (root / "data").mkdir()
            (root / "style" / "mapnik.xml").write_text("<Map></Map>")
            (root / "style" / "openstreetmap-carto.lua").write_text("-- lua")
            (root / "style" / "openstreetmap-carto.style").write_text("# style")
            (root / "style" / "indexes.sql").write_text("-- sql")
            (root / "data" / "region.osm.pbf").write_bytes(b"")
            (root / "manifest.json").write_text(
                json.dumps(
                    {
                        "name": "bundle",
                        "version": "1",
                        "style": {
                            "directory": "style",
                            "mapnikXml": "mapnik.xml",
                            "lua": "openstreetmap-carto.lua",
                            "styleFile": "openstreetmap-carto.style",
                            "indexesSql": "indexes.sql",
                        },
                        "imports": {"pbf": "data/region.osm.pbf"},
                    }
                )
            )
            config = minimal_config(map_bundle_uri=str(root))
            builder = CommandBuilder(config)
            command = builder.import_command({})
            indexes = builder.indexes_command({})

        self.assertIn("--tag-transform-script", command)
        self.assertIn("-S", command)
        self.assertTrue(command[-1].endswith("data/region.osm.pbf"))
        self.assertIsNotNone(indexes)
        self.assertTrue(indexes[-1].endswith("style/indexes.sql"))


if __name__ == "__main__":
    unittest.main()

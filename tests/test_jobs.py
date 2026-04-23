import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from tests.helpers import minimal_config
from tile_server.jobs import (
    CommandBuilder,
    CommandExecutionError,
    EXTERNAL_DATA_PLACEHOLDERS_SQL,
    JobError,
    JobRunner,
    JobStore,
)
from tile_server.tiles import TileRef


class _FakeCursor:
    def __init__(self, rows):
        self.rows = list(rows)
        self.executed = []
        self._row = None

    def execute(self, sql, params):
        self.executed.append((sql, params))
        self._row = self.rows.pop(0) if self.rows else None

    def fetchone(self):
        return self._row

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


class _FakeJobStore(JobStore):
    def __init__(self, rows):
        super().__init__("postgresql://example")
        self.cursor = _FakeCursor(rows)

    @contextmanager
    def connect(self):
        yield _FakeConnection(self.cursor)


class JobCommandTests(unittest.TestCase):
    def test_enqueue_dirty_tile_reports_created_status(self):
        store = _FakeJobStore([(123, True)])

        result = store.enqueue_dirty_tile(TileRef("default", 4, 9, 10), metatile_size=4)

        self.assertEqual(result.status, "created")
        self.assertTrue(result.created)
        sql, params = store.cursor.executed[0]
        self.assertIn("RETURNING id, (xmax = 0) AS created", sql)
        self.assertEqual(params[4:7], (8, 8, 4))

    def test_enqueue_dirty_tile_reports_coalesced_status(self):
        store = _FakeJobStore([(123, False)])

        result = store.enqueue_dirty_tile(TileRef("default", 4, 9, 10), metatile_size=4)

        self.assertEqual(result.status, "coalesced")
        self.assertTrue(result.coalesced)

    def test_enqueue_dirty_tile_reports_ignored_status_when_no_row_changes(self):
        store = _FakeJobStore([None])

        result = store.enqueue_dirty_tile(TileRef("default", 4, 9, 10), metatile_size=4)

        self.assertEqual(result.status, "ignored")
        self.assertTrue(result.ignored)

    def test_import_command_uses_external_database(self):
        config = minimal_config(import_database_url="postgresql://import:secret@db:5432/gis", import_threads=6)
        command = CommandBuilder(config).import_command({"pbf_uri": "/data/import/region.osm.pbf"})
        self.assertIn("--create", command)
        self.assertIn("-d", command)
        self.assertIn("postgresql://import:secret@db:5432/gis", command)
        self.assertIn("6", command)

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

    def test_import_input_validation_rejects_placeholder_bundle_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pbf = root / "region.osm.pbf"
            lua = root / "openstreetmap-carto.lua"
            style = root / "openstreetmap-carto.style"
            pbf.write_bytes(b"")
            lua.write_text("-- Placeholder for an offline map bundle. Replace with the bundle's trusted Lua transform.")
            style.write_text("# style")

            runner = JobRunner(minimal_config(dry_run=False), store=None)
            command = [
                "osm2pgsql",
                "-d",
                "postgresql://import:secret@db/gis",
                "--create",
                "--tag-transform-script",
                str(lua),
                "-S",
                str(style),
                str(pbf),
            ]

            with self.assertRaisesRegex(JobError, "placeholder file"):
                runner._validate_import_inputs(command, None)

    def test_indexes_command_uses_name_indexes_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            indexes = str(Path(tmp) / "indexes.sql")
            os.environ["NAME_INDEXES"] = indexes
            try:
                command = CommandBuilder(minimal_config()).indexes_command({"pbf_uri": "/data/import/region.osm.pbf"})
            finally:
                os.environ.pop("NAME_INDEXES", None)

        self.assertIsNotNone(command)
        self.assertEqual(command[-1], indexes)

    def test_command_execution_error_includes_stderr_tail(self):
        runner = JobRunner(minimal_config(dry_run=False), store=None)

        with self.assertRaises(CommandExecutionError) as raised:
            runner._run_command(["python3", "-c", "import sys; print('osm2pgsql detail', file=sys.stderr); sys.exit(7)"])

        self.assertEqual(raised.exception.result["returncode"], 7)
        self.assertIn("osm2pgsql detail", str(raised.exception))

    def test_external_data_placeholder_sql_covers_default_carto_tables(self):
        for table in (
            "simplified_water_polygons",
            "water_polygons",
            "icesheet_polygons",
            "icesheet_outlines",
            "ne_110m_admin_0_boundary_lines_land",
        ):
            self.assertIn(table, EXTERNAL_DATA_PLACEHOLDERS_SQL)
        self.assertIn("ice_edge text", EXTERNAL_DATA_PLACEHOLDERS_SQL)
        self.assertIn("geometry(MultiPolygon, 3857)", EXTERNAL_DATA_PLACEHOLDERS_SQL)
        self.assertIn("geometry(MultiLineString, 3857)", EXTERNAL_DATA_PLACEHOLDERS_SQL)

    def test_import_job_creates_external_data_placeholders_after_osm2pgsql(self):
        class PlaceholderStore:
            def __init__(self):
                self.created = False

            def ensure_external_data_placeholders(self):
                self.created = True

        with tempfile.TemporaryDirectory() as tmp:
            pbf = Path(tmp) / "region.osm.pbf"
            pbf.write_bytes(b"")
            store = PlaceholderStore()
            runner = JobRunner(minimal_config(dry_run=False), store=store)
            runner._validate_import_inputs = lambda command, indexes_command: None
            runner._run_command = lambda command: {"command": CommandBuilder.masked(command), "returncode": 0}

            result = runner._run_import({"pbf_uri": str(pbf)}, append=False, job_id="job")

        self.assertTrue(store.created)
        self.assertTrue(result["external_data_placeholders"])

    def test_external_data_load_job_uses_style_dir_and_creates_placeholders(self):
        class PlaceholderStore:
            def __init__(self):
                self.created = False

            def ensure_external_data_placeholders(self):
                self.created = True

        with tempfile.TemporaryDirectory() as tmp:
            style_dir = Path(tmp) / "style"
            style_dir.mkdir()
            store = PlaceholderStore()
            runner = JobRunner(minimal_config(dry_run=False), store=store)
            runner.external_data.load = lambda payload, style_dir, job_id, dry_run: {
                "status": "placeholder",
                "mode": "auto",
            }

            result = runner._run_external_data_load({"style_dir": str(style_dir)}, job_id="job")

        self.assertEqual(result["style_dir"], str(style_dir.resolve()))
        self.assertTrue(result["external_data_placeholders"])
        self.assertTrue(store.created)


if __name__ == "__main__":
    unittest.main()

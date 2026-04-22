import json
import multiprocessing
import tempfile
import unittest
from pathlib import Path

from tests.helpers import minimal_config
from tile_server.style import materialize_style_xml


MAPNIK_XML = """<Map>
  <Style name="landcover-low-zoom">
    <Rule>
      <PolygonPatternSymbolizer file="patterns/grey_vertical_hatch.svg" />
    </Rule>
  </Style>
  <Layer name="land">
    <Datasource>
      <Parameter name="type">postgis</Parameter>
      <Parameter name="host">old-host</Parameter>
      <Parameter name="dbname">old-db</Parameter>
    </Datasource>
  </Layer>
</Map>
"""


class StyleMaterializationTests(unittest.TestCase):
    def test_materialize_copies_pattern_and_symbol_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            style = root / "style-src"
            workdir = root / "work"
            (style / "patterns").mkdir(parents=True)
            (style / "symbols").mkdir()
            (style / "patterns" / "grey_vertical_hatch.svg").write_text("<svg/>")
            (style / "symbols" / "quarry.svg").write_text("<svg/>")
            (style / "mapnik.xml").write_text(MAPNIK_XML)

            target = materialize_style_xml(
                minimal_config(style_xml=str(style / "mapnik.xml"), style_workdir=str(workdir)),
                layer="default",
            )

            self.assertEqual(target.name, "mapnik-default.xml")
            self.assertEqual(target.parent.parent, workdir)
            self.assertTrue((target.parent / "patterns" / "grey_vertical_hatch.svg").is_file())
            self.assertTrue((target.parent / "symbols" / "quarry.svg").is_file())
            self.assertIn('file="patterns/grey_vertical_hatch.svg"', target.read_text())
            self.assertIn("style-cache-", str(target))

    def test_materialize_patches_only_copied_xml(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            style = root / "style-src"
            workdir = root / "work"
            style.mkdir()
            source = style / "mapnik.xml"
            source.write_text(MAPNIK_XML)

            target = materialize_style_xml(
                minimal_config(
                    style_xml=str(source),
                    style_workdir=str(workdir),
                    render_database_url="postgresql://render:secret@postgis:5432/gis",
                ),
                layer="default",
            )

            self.assertIn("old-host", source.read_text())
            self.assertIn("<Parameter name=\"host\">postgis</Parameter>", target.read_text())
            self.assertIn("<Parameter name=\"dbname\">gis</Parameter>", target.read_text())

    def test_materialize_bundle_style_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = root / "bundle"
            style = bundle / "style"
            workdir = root / "work"
            (style / "patterns").mkdir(parents=True)
            (style / "patterns" / "grey_vertical_hatch.svg").write_text("<svg/>")
            (style / "mapnik.xml").write_text(MAPNIK_XML)
            (bundle / "manifest.json").write_text(
                json.dumps({"name": "bundle", "version": "1", "style": {"directory": "style", "mapnikXml": "mapnik.xml"}})
            )

            target = materialize_style_xml(minimal_config(map_bundle_uri=str(bundle), style_workdir=str(workdir)))

            self.assertTrue(target.is_file())
            self.assertTrue((target.parent / "patterns" / "grey_vertical_hatch.svg").is_file())

    def test_materialize_normalizes_mapnik_incompatible_svg_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            style = root / "style-src"
            workdir = root / "work"
            (style / "symbols").mkdir(parents=True)
            (style / "mapnik.xml").write_text(MAPNIK_XML)
            (style / "symbols" / "guidepost.svg").write_text(
                """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="100%" height="100%" viewBox="0 0 14 14">
  <path d="M 0,0 14,14" />
</svg>
"""
            )
            (style / "symbols" / "bus_station.svg").write_text(
                """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14">
  <defs>
    <marker id="ArrowStart" markerWidth="4" markerHeight="3" refX="10" refY="5" orient="auto">
      <path d="M 10,0 0,5 10,10 Z" />
    </marker>
  </defs>
  <path d="M 1,1 13,13" />
</svg>
"""
            )

            target = materialize_style_xml(
                minimal_config(style_xml=str(style / "mapnik.xml"), style_workdir=str(workdir)),
                layer="default",
            )

            guidepost = (target.parent / "symbols" / "guidepost.svg").read_text()
            bus_station = (target.parent / "symbols" / "bus_station.svg").read_text()
            self.assertIn('width="14"', guidepost)
            self.assertIn('height="14"', guidepost)
            self.assertNotIn("<marker", bus_station)

    def test_materialize_normalizes_single_quoted_percent_dimensions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            style = root / "style-src"
            workdir = root / "work"
            (style / "symbols").mkdir(parents=True)
            (style / "mapnik.xml").write_text(MAPNIK_XML)
            (style / "symbols" / "single-quoted.svg").write_text(
                """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width='100%' height='100%' viewBox="0 0 16 12">
  <rect x="0" y="0" width="16" height="12" />
</svg>
"""
            )

            target = materialize_style_xml(
                minimal_config(style_xml=str(style / "mapnik.xml"), style_workdir=str(workdir)),
                layer="default",
            )

            svg = (target.parent / "symbols" / "single-quoted.svg").read_text()
            self.assertIn('width="16"', svg)
            self.assertIn('height="12"', svg)

    def test_materialize_is_safe_under_concurrent_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            style = root / "style-src"
            workdir = root / "work"
            (style / "patterns").mkdir(parents=True)
            (style / "patterns" / "grey_vertical_hatch.svg").write_text("<svg/>")
            (style / "mapnik.xml").write_text(MAPNIK_XML)

            queue = multiprocessing.get_context("spawn").Queue()
            processes = [
                multiprocessing.get_context("spawn").Process(
                    target=_materialize_worker,
                    args=(str(style / "mapnik.xml"), str(workdir), queue),
                )
                for _ in range(2)
            ]
            for process in processes:
                process.start()
            results = [queue.get(timeout=10) for _ in processes]
            for process in processes:
                process.join(timeout=10)

            targets = [Path(result["target"]) for result in results]
            self.assertEqual(targets[0], targets[1])
            self.assertTrue(targets[0].is_file())
            self.assertIn("<Map>", results[0]["text"])
            self.assertIn("patterns/grey_vertical_hatch.svg", results[0]["text"])


if __name__ == "__main__":
    unittest.main()


def _materialize_worker(style_xml: str, style_workdir: str, queue: multiprocessing.Queue) -> None:
    target = materialize_style_xml(
        minimal_config(style_xml=style_xml, style_workdir=style_workdir),
        layer="default",
    )
    queue.put({"target": str(target), "text": target.read_text()})
